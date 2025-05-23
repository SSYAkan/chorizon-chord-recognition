import os
from flask_cors import CORS
import tempfile
from pydub import AudioSegment
from pydub.utils import which
from flask import Flask, request, jsonify, render_template, Response
import redis
import io
import json
from datetime import datetime
from madmom.features.chords import CNNChordFeatureProcessor, CRFChordRecognitionProcessor
from madmom.features.chords import DeepChromaChordRecognitionProcessor
from madmom.audio.chroma import DeepChromaProcessor

app = Flask(__name__)
CORS(app)
# 设置最大文件上传大小为 50MB
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

# 错误处理
@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({'error': '文件太大，请上传小于 50MB 的文件'}), 413

# 连接 Redis
redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)

# 初始化 madmom 处理器
featproc = CNNChordFeatureProcessor()
decode = CRFChordRecognitionProcessor()
dcp = DeepChromaProcessor()

# 确保 ffmpeg 可用
AudioSegment.converter = which("ffmpeg")

# 音符索引字典
note_idx = {'C': 0, 'C#': 1, 'D': 2, 'D#': 3, 'E': 4, 'F': 5,
            'F#': 6, 'G': 7, 'G#': 8, 'A': 9, 'A#': 10, 'B': 11}

def extend_to_seventh_chords(triads, chroma, threshold=0.3):
    extended_chords = []
    
    for (start, end, chord) in triads:
        root, quality = (chord.split(':') + [None])[:2]
        if root not in note_idx:
            continue
        
        start_idx, end_idx = int(start * 10), int(end * 10)
        chroma_segment = chroma[start_idx:end_idx]
        root_idx = note_idx[root]
        
        major7_idx = (root_idx + 11) % 12
        minor7_idx = (root_idx + 10) % 12
        dom7_idx = minor7_idx
        dim7_idx = (root_idx + 9) % 12
        for chroma_vector in chroma_segment:
            # 如果是小三和弦，检查是否有小七度（m7）
            if quality == "min" and chroma_vector[minor7_idx] > threshold:
                chord = root + "m7"

            # 如果是大三和弦，优先检查是否有大七度（maj7），否则检查属七度（7）
            elif quality == "maj":
                if chroma_vector[major7_idx] > threshold:
                    chord = root + "maj7"
                elif chroma_vector[dom7_idx] > threshold:
                    chord = root + "7"

            # 如果是减三和弦（dim），需要区分 dim7 和 m7♭5（半减七）
            elif quality == "dim":
                if chroma_vector[minor7_idx] > threshold:  # 半减七（m7♭5）
                    chord = root + "m7♭5"
                elif chroma_vector[dim7_idx] > threshold:  # 减七（dim7）
                    chord = root + "dim7"
                
        extended_chords.append({"start_time": start, "end_time": end, "chord": chord})
    
    return extended_chords

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/chorizon')
def chorizon():
    return render_template('chorizon.html')

@app.route('/info')
def info():
    return render_template('info.html')

@app.route('/upload', methods=['POST'])
def process_audio():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    print(f"Processing file: {file.filename} with timestamp: {timestamp}")  # 调试日志
    
    file_bytes = io.BytesIO(file.read())  # 读取后保存到 BytesIO
    
    # 压缩音频
    try:
        sound = AudioSegment.from_file(file_bytes)
        # 压缩音频：降低比特率到 128kbps
        compressed_sound = sound.export(format="mp3", bitrate="128k")
        compressed_data = compressed_sound.read()
        redis_key = f"audio:{timestamp}:{file.filename}"  # 使用时间戳和文件名作为键
        print(f"Using Redis key for audio: {redis_key}")  # 调试日志

        # 存入 Redis
        redis_client.setex(redis_key, 3600, compressed_data)  # 保存1小时
        print(f"Saved compressed audio data to Redis, size: {len(compressed_data)} bytes")  # 调试日志
    except Exception as e:
        print(f"Error compressing audio: {str(e)}")  # 调试日志
        return jsonify({"error": "音频压缩失败: " + str(e)}), 500

    # 从 Redis 读取
    audio_data = redis_client.get(redis_key)
    if audio_data is None:
        print(f"Failed to retrieve audio data from Redis")  # 调试日志
        return jsonify({"error": "Redis 未找到音频数据"}), 500

    audio_bytes = io.BytesIO(audio_data)  # 转换为 BytesIO

    # 解析音频
    try:
        sound = AudioSegment.from_file(audio_bytes)
    except Exception as e:
        print(f"Error parsing audio: {str(e)}")  # 调试日志
        return jsonify({"error": "pydub 读取失败: " + str(e)}), 500

    # 创建临时文件
    tmp_wav_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
            tmp_wav_path = tmp_wav.name
            sound.export(tmp_wav_path, format="wav")
            print(f"Created temporary WAV file: {tmp_wav_path}")  # 调试日志

        # 进行和弦识别
        feats = featproc(tmp_wav_path)
        triads = decode(feats)
        chroma = dcp(tmp_wav_path)
        extended_chords = extend_to_seventh_chords(triads, chroma, 0.2)
        print(f"Found {len(extended_chords)} chords")  # 调试日志

        # 保存到历史记录
        history_key = f"history:{timestamp}"
        history_data = {
            "filename": file.filename,
            "timestamp": timestamp,  # 使用相同的时间戳格式
            "chords": extended_chords,
            "audio_key": redis_key  # 添加音频键到历史记录
        }
        redis_client.setex(history_key, 3600, json.dumps(history_data))  # 保存1小时
        print(f"Saved history data to Redis with key: {history_key}")  # 调试日志

    except Exception as e:
        print(f"Error in processing: {str(e)}")  # 调试日志
        return jsonify({"error": "madmom 处理失败: " + str(e)}), 500

    finally:
        # 确保删除临时文件
        if tmp_wav_path and os.path.exists(tmp_wav_path):
            os.remove(tmp_wav_path)
            print(f"Removed temporary file: {tmp_wav_path}")  # 调试日志

    return jsonify({'chords': extended_chords})

@app.route('/history', methods=['GET'])
def get_history():
    try:
        # 获取所有历史记录
        history_keys = redis_client.keys("history:*")
        history_list = []
        
        for key in history_keys:
            history_data = redis_client.get(key)
            if history_data:
                history_list.append(json.loads(history_data))
        
        # 按时间戳排序，最新的在前
        history_list.sort(key=lambda x: x['timestamp'], reverse=True)
        
        return jsonify({'history': history_list})
    except Exception as e:
        return jsonify({"error": f"获取历史记录失败: {str(e)}"}), 500

@app.route('/get_audio/<timestamp>')
def get_audio(timestamp):
    try:
        print(f"Getting audio for timestamp: {timestamp}")  # 调试日志
        
        # 从历史记录中获取音频键
        history_key = f"history:{timestamp}"
        print(f"Looking for history key: {history_key}")  # 调试日志
        
        history_data = redis_client.get(history_key)
        if not history_data:
            print(f"History not found for key: {history_key}")  # 调试日志
            return jsonify({"error": "未找到历史记录"}), 404
            
        history = json.loads(history_data)
        print(f"Found history data: {history}")  # 调试日志
        
        audio_key = history.get('audio_key')
        if not audio_key:
            print(f"Audio key not found in history: {history}")  # 调试日志
            return jsonify({"error": "未找到音频键"}), 404
        
        print(f"Looking for audio key: {audio_key}")  # 调试日志
        
        # 从 Redis 获取音频数据
        audio_data = redis_client.get(audio_key)
        if not audio_data:
            print(f"Audio data not found for key: {audio_key}")  # 调试日志
            return jsonify({"error": "未找到音频数据"}), 404
            
        print(f"Found audio data, size: {len(audio_data)} bytes")  # 调试日志
            
        # 根据文件扩展名确定 MIME 类型
        mime_type = 'audio/mpeg'  # 默认值
        if history['filename'].lower().endswith('.wav'):
            mime_type = 'audio/wav'
        elif history['filename'].lower().endswith('.mp3'):
            mime_type = 'audio/mpeg'
        elif history['filename'].lower().endswith('.ogg'):
            mime_type = 'audio/ogg'
            
        print(f"Using MIME type: {mime_type}")  # 调试日志
            
        # 返回音频数据
        return Response(audio_data, mimetype=mime_type)
    except Exception as e:
        print(f"Error in get_audio: {str(e)}")  # 调试日志
        return jsonify({"error": f"获取音频数据失败: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True, use_reloader=True, reloader_type='auto')
