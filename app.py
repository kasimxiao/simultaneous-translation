from flask import Flask, render_template, request, Response, jsonify
from flask_socketio import SocketIO, emit
import base64
import threading
import asyncio
import queue
import boto3
import json
import time
import os
import urllib.request
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')
socketio = SocketIO(app, cors_allowed_origins="*")

# Cognito configuration
COGNITO_REGION = os.environ.get('COGNITO_REGION', 'ap-northeast-1')
COGNITO_USER_POOL_ID = os.environ.get('COGNITO_USER_POOL_ID', '')
COGNITO_CLIENT_ID = os.environ.get('COGNITO_CLIENT_ID', '')
COGNITO_DOMAIN = os.environ.get('COGNITO_DOMAIN', '')
AUTH_ENABLED = bool(COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID)

# JWKS 缓存
_jwks_cache = {'keys': None, 'fetched_at': 0}


def _fetch_jwks():
    """从 Cognito 获取 JWKS 并缓存"""
    now = time.time()
    if _jwks_cache['keys'] and now - _jwks_cache['fetched_at'] < 3600:
        return _jwks_cache['keys']
    url = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())
    _jwks_cache['keys'] = {k['kid']: k for k in data['keys']}
    _jwks_cache['fetched_at'] = now
    return _jwks_cache['keys']


def verify_cognito_token(token):
    """验证 Cognito JWT token，成功返回 payload，失败返回 None"""
    if not AUTH_ENABLED:
        return {'sub': 'anonymous'}
    if not token:
        return None
    try:
        from jose import jwt
        headers = jwt.get_unverified_headers(token)
        kid = headers.get('kid')
        keys = _fetch_jwks()
        key = keys.get(kid)
        if not key:
            # 可能 JWKS 轮换，强制刷新一次
            _jwks_cache['fetched_at'] = 0
            keys = _fetch_jwks()
            key = keys.get(kid)
            if not key:
                return None
        # id_token 的 aud = client_id，access_token 没有 aud 但有 client_id claim
        claims = jwt.get_unverified_claims(token)
        token_use = claims.get('token_use')
        if token_use == 'id':
            payload = jwt.decode(
                token, key,
                algorithms=['RS256'],
                audience=COGNITO_CLIENT_ID,
                issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}",
                options={'verify_at_hash': False},
            )
        else:
            payload = jwt.decode(
                token, key,
                algorithms=['RS256'],
                options={'verify_aud': False},
                issuer=f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}",
            )
            if payload.get('client_id') != COGNITO_CLIENT_ID:
                return None
        return payload
    except Exception as e:
        print(f"Token 验证失败: {e}")
        return None


def extract_bearer_token():
    """从 Authorization header 提取 Bearer token"""
    auth = request.headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


# 存储每个 socket 连接的认证状态
_authed_sids = set()

audio_queue = queue.Queue()
transcription_active = False
current_mode = "en2zh"  # "en2zh" 或 "zh2en"

def translate_with_bedrock(text, mode="en2zh"):
    """使用Bedrock Claude翻译文本"""
    try:
        bedrock = boto3.client('bedrock-runtime', region_name='ap-northeast-1')
        
        if mode == "en2zh":
            prompt = f"""你是一个专业的同声传译员。请将以下英文语音识别结果翻译成自然流畅的中文。直接输出翻译结果，只输出一个版本，不要解释，不要提供多个选项。
翻译文本:
{text}"""
        else:
            prompt = f"""You are a professional simultaneous interpreter. Translate the following Chinese speech recognition result into natural, fluent English. Output only the translation, one version only, no explanation, no alternatives.
translate text:
{text}"""
        
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1000,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }
        
        response = bedrock.invoke_model(
            modelId='global.anthropic.claude-haiku-4-5-20251001-v1:0',
            body=json.dumps(body)
        )
        
        result = json.loads(response['body'].read())
        return result['content'][0]['text']
        
    except Exception as e:
        print(f"翻译错误: {e}")
        return f"翻译失败: {str(e)}"

class MyEventHandler(TranscriptResultStreamHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_sent_transcript = ""  # 当前 segment 内已发送的文本
        self.current_partial = ""  # 当前显示的未完成片段
        self.pending_final = ""  # 暂存过短的 final 结果
        self.current_result_id = None  # 当前 segment 的 result_id
    
    def _find_split_pos(self, transcript, unsent_start):
        """在未发送部分查找断句位置，返回 -1 表示没找到"""
        
        if current_mode == "zh2en":
            end_marks_set = set(['。', '？', '！', '?', '!'])
            pause_marks_set = set(['，', '、', '；', ','])
            
            # 1. 在未发送部分找句末标点
            for i in range(len(transcript) - 1, unsent_start - 1, -1):
                if transcript[i] in end_marks_set:
                    return i
            
            # 2. 在未发送部分找第2个停顿标点
            pause_count = 0
            for i in range(unsent_start, len(transcript)):
                if transcript[i] in pause_marks_set:
                    pause_count += 1
                    if pause_count >= 2:
                        print(f"[PAUSE] 第2个停顿标点在位置 {i}: '{transcript[i]}'")
                        return i
            
            # 3. 长度兜底：未发送超过40字符
            if len(transcript) - unsent_start >= 40:
                print(f"[FORCE] 中文长度兜底断句，未发送长度: {len(transcript) - unsent_start}")
                return len(transcript) - 1
        else:
            # 英文模式：在未发送部分找断句标点
            # 句号/问号/叹号：单词数>=5 断句
            # 逗号：单词数>=5 也断句（避免句子过长）
            end_marks_set = set(['.', '?', '!'])
            pause_marks_set = set([','])
            
            # 从前往后找，优先在最早的合适位置断句
            for i in range(unsent_start, len(transcript)):
                if transcript[i] in end_marks_set or transcript[i] in pause_marks_set:
                    candidate = transcript[unsent_start:i+1].strip()
                    word_count = len(candidate.split())
                    if word_count >= 5:
                        if transcript[i] in pause_marks_set:
                            print(f"[COMMA] 英文逗号断句({word_count}词): {candidate}")
                        return i
                    # 句号/问号/叹号即使不够5个词也记录位置，但不断
                    # 继续找下一个标点
        
        return -1
    
    def _send_segment(self, text):
        """发送一个片段并启动翻译"""
        print(f"[SEND] {text}")
        socketio.emit('new_segment', {'text': text})
        
        def translate_worker(t=text, m=current_mode):
            translation = translate_with_bedrock(t, m)
            socketio.emit('translation', {'text': translation})
        
        thread = threading.Thread(target=translate_worker)
        thread.daemon = True
        thread.start()
    
    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        results = transcript_event.transcript.results
        for result in results:
            for alt in result.alternatives:
                transcript = alt.transcript.strip()
                if not transcript:
                    continue
                
                # 检测新 segment：result_id 变化时重置状态
                rid = result.result_id
                if rid != self.current_result_id:
                    if self.current_result_id is not None and not rid.startswith(self.current_result_id.split('.')[0] if '.' in self.current_result_id else self.current_result_id):
                        # 全新的 segment，重置
                        print(f"[NEW_SEG] 新segment: {rid}, 重置 last_sent")
                        self.last_sent_transcript = ""
                    self.current_result_id = rid
                
                print(f'[RAW] rid={rid} transcript: "{transcript[:80]}...", is_partial: {result.is_partial}')
                
                # 计算未发送文本的起始位置
                if transcript.startswith(self.last_sent_transcript):
                    unsent_start = len(self.last_sent_transcript)
                elif self.last_sent_transcript:
                    # 尝试忽略末尾标点差异（Transcribe 可能把逗号修正为句号）
                    last_sent_stripped = self.last_sent_transcript.rstrip('.,!?;:，。！？；')
                    if last_sent_stripped and transcript.startswith(last_sent_stripped):
                        # 找到 transcript 中对应位置后的第一个非标点字符
                        pos = len(last_sent_stripped)
                        while pos < len(transcript) and transcript[pos] in '.,!?;:，。！？； ':
                            pos += 1
                        unsent_start = pos
                        # 更新 last_sent 为实际匹配的部分
                        self.last_sent_transcript = transcript[:pos]
                        print(f"[FIX] 标点修正匹配，新unsent_start={pos}")
                    else:
                        print(f"[RESET] 文本不匹配，重置 last_sent")
                        unsent_start = 0
                        self.last_sent_transcript = ""
                else:
                    unsent_start = 0
                
                unsent_text = transcript[unsent_start:].strip()
                
                if result.is_partial:
                    # 部分结果：检查未发送部分是否有断句标点
                    split_pos = self._find_split_pos(transcript, unsent_start)
                    
                    if split_pos >= unsent_start:
                        # 找到断句点
                        complete_part = transcript[:split_pos + 1].strip()
                        remaining_part = transcript[split_pos + 1:].strip()
                        
                        new_content = complete_part[len(self.last_sent_transcript):].strip()
                        
                        # 如果有暂存的内容，合并
                        if self.pending_final:
                            new_content = self.pending_final + " " + new_content if new_content else self.pending_final
                            self.pending_final = ""
                        
                        if new_content:
                            self._send_segment(new_content)
                            self.last_sent_transcript = complete_part
                        
                        # 显示剩余部分
                        if remaining_part:
                            socketio.emit('partial_transcript', {'text': remaining_part})
                            self.current_partial = remaining_part
                        else:
                            self.current_partial = ""
                    else:
                        # 没有断句点，显示为 partial
                        if unsent_text and unsent_text != self.current_partial:
                            display = unsent_text
                            if self.pending_final:
                                display = self.pending_final + " " + display
                            socketio.emit('partial_transcript', {'text': display})
                            self.current_partial = unsent_text
                
                else:
                    # 最终结果（is_partial=False）
                    new_content = unsent_text
                    
                    # 合并暂存内容
                    if self.pending_final:
                        new_content = self.pending_final + " " + new_content if new_content else self.pending_final
                        self.pending_final = ""
                    
                    if new_content:
                        # 判断是否太短（少于3个单词且没有标点）
                        word_count = len(new_content.split()) if current_mode != "zh2en" else len(new_content)
                        has_end_punct = any(new_content.endswith(p) for p in ['.', '?', '!', '。', '？', '！'])
                        
                        if current_mode != "zh2en" and word_count < 5 and not has_end_punct:
                            # 英文模式下太短且没标点，暂存等下一段合并
                            print(f"[HOLD] 暂存过短片段: {new_content}")
                            self.pending_final = new_content
                            self.last_sent_transcript = transcript
                            self.current_partial = ""
                        else:
                            self._send_segment(new_content)
                            self.last_sent_transcript = transcript
                            self.current_partial = ""

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/callback')
def callback():
    """Cognito OAuth callback - 前端处理 token"""
    return render_template('index.html')


@app.route('/cognito-config')
def cognito_config():
    """返回 Cognito 配置给前端"""
    return jsonify({
        'region': COGNITO_REGION,
        'userPoolId': COGNITO_USER_POOL_ID,
        'clientId': COGNITO_CLIENT_ID,
        'domain': COGNITO_DOMAIN,
    })


@app.route('/tts', methods=['POST'])
def text_to_speech():
    """使用 Polly 将文本转为语音"""
    # 验证 token
    if AUTH_ENABLED:
        token = extract_bearer_token()
        if not verify_cognito_token(token):
            return Response('Unauthorized', status=401)

    try:
        data = request.get_json()
        text = data.get('text', '')
        lang = data.get('lang', 'zh')
        if not text:
            return Response('No text', status=400)

        polly = boto3.client('polly', region_name='ap-northeast-1')
        
        if lang == 'en':
            voice_id = 'Matthew'
        else:
            voice_id = 'Zhiyu'
        
        response = polly.synthesize_speech(
            Text=text,
            OutputFormat='mp3',
            VoiceId=voice_id,
            Engine='neural',
        )

        audio_stream = response['AudioStream'].read()
        return Response(audio_stream, mimetype='audio/mpeg')

    except Exception as e:
        print(f"TTS错误: {e}")
        return Response(str(e), status=500)

async def basic_transcribe():
    global transcription_active
    print(f"等待音频数据... 模式: {current_mode}")
    
    # 等待第一个音频块到达（最多等待10秒）
    first_chunk = None
    wait_start = time.time()
    while transcription_active and not first_chunk:
        try:
            first_chunk = audio_queue.get(timeout=0.1)
        except queue.Empty:
            if time.time() - wait_start > 10:
                raise Exception("等待音频数据超时（10秒）")
            await asyncio.sleep(0.1)
    
    if not first_chunk:
        print("未收到音频数据，退出")
        return
    
    print(f"收到第一个音频块，大小: {len(first_chunk)}")
    
    lang_code = "zh-CN" if current_mode == "zh2en" else "en-US"
    print(f"开始转录... 语言: {lang_code}")
    
    try:
        # 创建客户端
        client = TranscribeStreamingClient(region="ap-northeast-1")
        
        # 启动转录流
        stream = await client.start_stream_transcription(
            language_code=lang_code,
            media_sample_rate_hz=16000,
            media_encoding="pcm",
            enable_partial_results_stabilization=True,
            partial_results_stability="high",
        )
        
        print("转录流已启动")
        
        async def write_chunks():
            chunk_count = 0
            
            # 先发送第一个音频块
            try:
                chunk_count += 1
                print(f"发送音频块 {chunk_count}, 大小: {len(first_chunk)}")
                await stream.input_stream.send_audio_event(audio_chunk=first_chunk)
            except Exception as e:
                print(f"发送第一个音频块错误: {e}")
                return
            
            last_chunk_time = time.time()
            
            # 继续发送后续音频块
            while transcription_active:
                try:
                    chunk = await asyncio.get_event_loop().run_in_executor(
                        None, audio_queue.get, True, 0.1
                    )
                    if chunk:
                        chunk_count += 1
                        last_chunk_time = time.time()
                        if chunk_count % 10 == 0:  # 每10个块打印一次
                            print(f"发送音频块 {chunk_count}, 大小: {len(chunk)}")
                        await stream.input_stream.send_audio_event(audio_chunk=chunk)
                except queue.Empty:
                    # 检查是否长时间没有音频数据
                    if time.time() - last_chunk_time > 30:
                        print("[WARN] 30秒内没有收到音频数据")
                        break
                    await asyncio.sleep(0.01)
                except Exception as e:
                    print(f"发送音频错误: {e}")
                    break
            
            print(f"音频发送完成，共发送 {chunk_count} 个块")
            try:
                await stream.input_stream.end_stream()
            except Exception as e:
                print(f"关闭流错误: {e}")
        
        handler = MyEventHandler(stream.output_stream)
        
        # 并发执行写入和处理
        await asyncio.gather(write_chunks(), handler.handle_events())
        
    except Exception as e:
        print(f"转录流错误: {e}")
        import traceback
        traceback.print_exc()
        raise

@socketio.on('connect')
def handle_connect(auth=None):
    if not AUTH_ENABLED:
        _authed_sids.add(request.sid)
        return True
    token = None
    if auth and isinstance(auth, dict):
        token = auth.get('token')
    if not token:
        # 备用：从 query string 或 header 取
        token = request.args.get('token') or extract_bearer_token()
    payload = verify_cognito_token(token)
    if not payload:
        print(f"Socket 连接未授权，拒绝: sid={request.sid}")
        return False  # 拒绝连接
    _authed_sids.add(request.sid)
    print(f"Socket 连接已授权: sid={request.sid}, user={payload.get('email') or payload.get('sub')}")
    return True


@socketio.on('disconnect')
def handle_disconnect():
    _authed_sids.discard(request.sid)


@socketio.on('audio_data')
def handle_audio(data):
    if AUTH_ENABLED and request.sid not in _authed_sids:
        return
    try:
        audio_data = base64.b64decode(data['audio'])
        if len(audio_data) > 100:
            audio_queue.put(audio_data)
    except Exception as e:
        print(f"音频处理错误: {e}")

@socketio.on('start_transcription')
def start_transcription(data=None):
    global transcription_active, current_mode

    if AUTH_ENABLED and request.sid not in _authed_sids:
        socketio.emit('error', {'text': 'Unauthorized'}, to=request.sid)
        return
    
    # 接收模式参数
    if data and isinstance(data, dict):
        current_mode = data.get('mode', 'en2zh')
    else:
        current_mode = 'en2zh'
    
    # 清空音频队列
    while not audio_queue.empty():
        try:
            audio_queue.get_nowait()
        except queue.Empty:
            break
    
    transcription_active = True
    print(f"启动转录服务，模式: {current_mode}")
    
    # 验证AWS凭证
    try:
        import boto3
        sts = boto3.client('sts', region_name='ap-northeast-1')
        identity = sts.get_caller_identity()
        print(f"AWS身份验证成功: {identity['Account']}")
    except Exception as e:
        error_msg = f"AWS凭证验证失败: {str(e)}"
        print(error_msg)
        socketio.emit('error', {'text': error_msg})
        return
    
    def run_transcription():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(basic_transcribe())
        except Exception as e:
            error_msg = f"转录错误: {str(e)}"
            print(error_msg)
            import traceback
            traceback.print_exc()
            socketio.emit('error', {'text': error_msg})
        finally:
            loop.close()
    
    thread = threading.Thread(target=run_transcription)
    thread.daemon = True
    thread.start()

@socketio.on('stop_transcription')
def stop_transcription():
    global transcription_active
    transcription_active = False
    print("停止转录服务")

if __name__ == '__main__':
    print("启动AWS Transcribe英文转录 + Bedrock翻译应用")
    print("访问 http://localhost:8080")
    socketio.run(app, debug=False, port=8080, host='0.0.0.0', allow_unsafe_werkzeug=True)
