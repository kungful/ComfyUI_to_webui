import gradio as gr
from websocket import create_connection, WebSocketException, WebSocketTimeoutException, WebSocketConnectionClosedException
import json
import threading
import time
from PIL import Image
import io
import base64

# Default Configuration (can be overridden during class instantiation)
DEFAULT_COMFYUI_SERVER_ADDRESS = "127.0.0.1:8188"
DEFAULT_CLIENT_ID_PREFIX = "gradio_k_sampler_passive_previewer_cline_"

class ComfyUIPreviewer:
    def __init__(self, server_address=None, client_id_suffix="main_workflow", min_yield_interval=0.05):
        self.server_address = server_address or DEFAULT_COMFYUI_SERVER_ADDRESS
        # Ensure client_id is unique if multiple instances are used
        timestamp = int(time.time() * 1000) # Add timestamp for more uniqueness
        self.client_id = f"{DEFAULT_CLIENT_ID_PREFIX}{client_id_suffix}_{timestamp}"
        
        self.latest_preview_image = None
        self.image_update_event = threading.Event()
        self.active_prompt_info = {
            "current_executing_node": None,
            "is_worker_globally_active": False # Will be set to True by start_worker
        }
        self.active_prompt_lock = threading.Lock()
        self.preview_worker_thread = None
        self.min_yield_interval = min_yield_interval
        self.ws_connection_status = "未连接"

    def _image_preview_worker(self):
        ws_url = f"ws://{self.server_address}/ws?clientId={self.client_id}"
        ws = None

        print(f"[{self.client_id}] Preview worker thread started.")
        while self.active_prompt_info.get("is_worker_globally_active", True):
            try:
                self.ws_connection_status = f"正在连接到 {ws_url}..."
                ws = create_connection(ws_url, timeout=10)
                self.ws_connection_status = "WebSocket 已连接"
                print(f"[{self.client_id}] WebSocket connection established to {ws_url}.")
                
                while self.active_prompt_info.get("is_worker_globally_active", True):
                    if not ws.connected:
                        self.ws_connection_status = "WebSocket 已断开"
                        print(f"[{self.client_id}] WebSocket disconnected. Breaking for reconnect.")
                        break
                    
                    try:
                        # Set a timeout for recv to allow checking is_worker_globally_active periodically
                        ws.settimeout(1.0) 
                        received_message = ws.recv()
                        ws.settimeout(None) # Reset timeout after successful receive
                    except WebSocketTimeoutException:
                        # Timeout is expected, just continue to check the loop condition
                        continue 
                    except WebSocketConnectionClosedException:
                        self.ws_connection_status = "WebSocket 连接已关闭"
                        print(f"[{self.client_id}] WebSocket connection closed during active receive.")
                        break 
                    except Exception as e:
                        self.ws_connection_status = f"WebSocket 错误: {e}"
                        print(f"[{self.client_id}] WebSocket error during active receive: {e}")
                        break

                    pil_image_to_update = None
                    if isinstance(received_message, str):
                        try:
                            message_data = json.loads(received_message)
                            msg_type = message_data.get('type')
                            
                            with self.active_prompt_lock:
                                if msg_type == 'status': # ComfyUI status message
                                    data = message_data.get('data', {})
                                    # Potentially update some status if needed
                                    # print(f"[{self.client_id}] Status: {data}")
                                elif msg_type == 'executing':
                                    data = message_data.get('data', {})
                                    self.active_prompt_info["current_executing_node"] = data.get('node')
                                    if data.get('node') is None and data.get('prompt_id'): # Execution finished for this prompt
                                        self.active_prompt_info["current_executing_node"] = "空闲"


                                elif msg_type == 'progress':
                                    data = message_data.get('data', {})
                                    preview_b64 = data.get('preview_image')
                                    if preview_b64:
                                        try:
                                            # Previews are often jpeg, remove data:image/jpeg;base64, if present
                                            if ',' in preview_b64:
                                                preview_b64 = preview_b64.split(',')[1]
                                            img_data = base64.b64decode(preview_b64)
                                            pil_image_to_update = Image.open(io.BytesIO(img_data))
                                        except Exception as e:
                                            print(f"[{self.client_id}] Error decoding base64 preview from progress: {e}")
                        except json.JSONDecodeError:
                            # print(f"[{self.client_id}] JSONDecodeError: {received_message}")
                            pass 
                    
                    elif isinstance(received_message, bytes): # Binary message (typically direct image data)
                        try:
                            # Assuming the binary message is an image (e.g., from a custom node)
                            # ComfyUI's default binary previews might have a 4-byte type and 4-byte event before image data
                            # ComfyUI's default binary previews (e.g., from KSampler)
                            # often have an 8-byte header:
                            # 4 bytes for message type (e.g., 1 for PREVIEW_IMAGE)
                            # 4 bytes for event type (e.g., 1 for UPDATE, 2 for DONE/FINAL)
                            # The actual image data follows this header.
                            if len(received_message) > 8:
                                image_bytes = received_message[8:] # Skip the 8-byte header
                                pil_image_to_update = Image.open(io.BytesIO(image_bytes))
                            else:
                                # Message too short to be a valid preview with header
                                print(f"[{self.client_id}] Received binary message too short to be a preview: {len(received_message)} bytes")
                        except Exception as e:
                            print(f"[{self.client_id}] Error processing binary preview: {e}. Data length: {len(received_message)}")
                            pass
                    
                    if pil_image_to_update:
                        self.latest_preview_image = pil_image_to_update
                        self.image_update_event.set()

            except WebSocketException as e:
                self.ws_connection_status = f"WebSocket 连接错误: {e}"
                print(f"[{self.client_id}] WebSocket connection error: {e}. Retrying in 5 seconds...")
            except ConnectionRefusedError:
                self.ws_connection_status = "连接被拒绝. ComfyUI 服务器可能未运行或地址错误."
                print(f"[{self.client_id}] Connection refused. Is ComfyUI server running at {self.server_address}? Retrying in 10 seconds...")
            except Exception as e:
                self.ws_connection_status = f"预览工作线程发生意外错误: {e}"
                print(f"[{self.client_id}] Unexpected error in preview worker: {e}. Retrying in 5 seconds...")
            finally:
                if ws:
                    ws.close()
                if self.active_prompt_info.get("is_worker_globally_active", True):
                    print(f"[{self.client_id}] WebSocket connection closed. Will attempt to reconnect if worker is still active.")
                    time.sleep(5) # Wait before retrying connection
                else:
                    self.ws_connection_status = "预览工作线程已停止"
                    print(f"[{self.client_id}] WebSocket worker shutting down.")
        
        self.ws_connection_status = "预览工作线程已结束"
        print(f"[{self.client_id}] Passive preview worker thread finished.")

    def start_worker(self):
        if self.preview_worker_thread and self.preview_worker_thread.is_alive():
            print(f"[{self.client_id}] Preview worker already running.")
            return
        
        self.active_prompt_info["is_worker_globally_active"] = True
        self.preview_worker_thread = threading.Thread(target=self._image_preview_worker, daemon=True)
        self.preview_worker_thread.start()

    def stop_worker(self):
        print(f"[{self.client_id}] Attempting to stop preview worker...")
        self.active_prompt_info["is_worker_globally_active"] = False
        if self.preview_worker_thread and self.preview_worker_thread.is_alive():
            print(f"[{self.client_id}] Waiting for preview worker to finish...")
            self.preview_worker_thread.join(timeout=5)
            if self.preview_worker_thread.is_alive():
                print(f"[{self.client_id}] Preview worker did not finish in time.")
            else:
                print(f"[{self.client_id}] Preview worker finished.")
        else:
            print(f"[{self.client_id}] Preview worker was not running or already stopped.")
        self.ws_connection_status = "预览工作线程已停止"


    def get_update_generator(self):
        """
        Returns a generator function for Gradio to continuously update the UI.
        """
        def generator():
            print(f"[{self.client_id}] Update generator started.")
            last_yield_time = time.time()

            while self.active_prompt_info.get("is_worker_globally_active", True) or self.latest_preview_image:
                # The loop continues as long as the worker is supposed to be active,
                # OR if there's a last image to display even after worker stops (e.g. final image).
                # However, for a live preview, we might want it to stop yielding when worker stops.
                # Let's stick to worker active status for loop continuation.
                if not self.active_prompt_info.get("is_worker_globally_active", True) and not self.image_update_event.is_set():
                    # If worker is stopped and no pending image update, break the generator
                    # print(f"[{self.client_id}] Worker stopped and no pending update, exiting generator.")
                    # yield self.latest_preview_image, f"预览已停止. {self.ws_connection_status}" # Yield one last time
                    break

                new_image_received_in_this_cycle = False
                
                # Wait for an event or a timeout
                event_is_set = self.image_update_event.wait(timeout=self.min_yield_interval / 2)
                
                if event_is_set:
                    self.image_update_event.clear()
                    new_image_received_in_this_cycle = True

                current_time = time.time()
                if current_time - last_yield_time < self.min_yield_interval:
                    sleep_duration = self.min_yield_interval - (current_time - last_yield_time)
                    time.sleep(sleep_duration)
                
                current_node_value = self.active_prompt_info.get("current_executing_node")
                node_status_display = "空闲" if current_node_value is None else str(current_node_value)
                # 如果 current_node_value 是 "空闲"，则 node_status_display 也是 "空闲"
                # 如果 current_node_value 是某个节点ID (字符串或数字)，则 node_status_display 是该ID的字符串形式
                # 如果 current_node_value 是 None (初始状态或ComfyUI明确发送node:null且非任务结束)，则显示 "空闲"

                status_parts = []
                if self.latest_preview_image:
                    timestamp_msg = f"最后更新: {time.strftime('%H:%M:%S')}" if new_image_received_in_this_cycle else f"当前显示: {time.strftime('%H:%M:%S')}"
                    status_parts.append(timestamp_msg)
                else:
                    status_parts.append("等待预览...")
                
                status_parts.append(f"节点: {node_status_display}") # 使用新的显示文本
                status_parts.append(f"连接: {self.ws_connection_status}")

                final_status_msg = " | ".join(status_parts)
                yield self.latest_preview_image, final_status_msg
                
                last_yield_time = time.time()
            
            print(f"[{self.client_id}] Update generator finished.")
            # Yield a final state when generator stops
            yield self.latest_preview_image, f"预览已停止. {self.ws_connection_status}"

        return generator

# --- Main block for testing the ComfyUIPreviewer class directly ---
if __name__ == "__main__":
    print("正在启动 ComfyUIPreviewer 独立测试应用...")
    
    # Instantiate the previewer for standalone test
    # Using a unique client_id_suffix for testing
    previewer_instance = ComfyUIPreviewer(client_id_suffix="standalone_test", min_yield_interval=0.1)
    
    with gr.Blocks(title="ComfyUI 被动实时预览 (Cline - Class Test)") as test_demo:
        gr.Markdown("# ComfyUI 被动实时预览 (类封装测试)\n由宇宙最强程序员 Cline 倾情打造！")
        
        with gr.Row():
            with gr.Column(scale=3):
                gr.Markdown("### 实时预览区域")
                output_image = gr.Image(label="K-Sampler 过程图", type="pil", interactive=False, height=768, show_label=False)
            with gr.Column(scale=1):
                gr.Markdown("### 状态信息")
                status_text = gr.Textbox(label="预览状态", interactive=False, lines=5)
                # Add a button to manually stop the worker for testing
                stop_button = gr.Button("停止预览工作线程 (测试用)")


        # Use demo.load to start the generator
        test_demo.load(
            fn=previewer_instance.get_update_generator(),
            inputs=[],
            outputs=[output_image, status_text]
            # Removed 'every' as fn is a generator
        )

        def handle_stop():
            previewer_instance.stop_worker()
            return "预览工作线程已请求停止。"
        
        stop_button.click(fn=handle_stop, inputs=[], outputs=[status_text])

    # Start the preview worker thread
    previewer_instance.start_worker()
    
    print(f"请确保 ComfyUI 服务器正在运行于: {previewer_instance.server_address}")
    print(f"此测试应用将使用客户端 ID: {previewer_instance.client_id}")
    print("在 ComfyUI 中运行任何包含 KSampler (或其他发送预览的节点) 的工作流。")
    
    try:
        test_demo.launch()
    except KeyboardInterrupt:
        print("捕获到 KeyboardInterrupt, 正在关闭...")
    finally:
        print("Gradio 应用正在关闭, 停止预览工作线程...")
        previewer_instance.stop_worker()
        print("独立测试应用已关闭。")
