import os
import numpy as np
from PIL import Image, PngImagePlugin
import folder_paths
from .hua_icons import icons
import json
import imageio # 用于 GIF/WebP 写入
import subprocess
import sys
import datetime
import re
import torch
import itertools
# 尝试导入 LoraLoader 以便处理潜在的 VAE 输入，如果不存在则忽略
try:
    from nodes import LoraLoader
except ImportError:
    print("无法导入 nodes.LoraLoader，VAE 相关功能（如果未来添加）可能受限。")

# 尝试导入 VideoHelperSuite 的 LazyAudioMap，如果失败则优雅处理
try:
    from comfyui_videohelpersuite.videohelpersuite.utils import LazyAudioMap
    print("成功导入 LazyAudioMap from comfyui_videohelpersuite.")
    HAS_LAZY_AUDIO_MAP = True
except ImportError:
    print("无法导入 LazyAudioMap from comfyui_videohelpersuite。将仅支持标准字典格式的音频输入。")
    LazyAudioMap = None # 定义为 None 以便 isinstance 检查安全失败
    HAS_LAZY_AUDIO_MAP = False

# --- Constants and Setup ---
OUTPUT_DIR = folder_paths.get_output_directory()
TEMP_DIR = folder_paths.get_temp_directory()
os.makedirs(TEMP_DIR, exist_ok=True) # 确保临时目录存在

# --- FFMPEG Path ---
ffmpeg_path = "ffmpeg" # 默认使用系统路径中的 ffmpeg
try:
    # 尝试从环境变量获取 FFMPEG_PATH
    ffmpeg_path_env = os.getenv("FFMPEG_PATH")
    if ffmpeg_path_env and os.path.exists(ffmpeg_path_env):
        ffmpeg_path = ffmpeg_path_env
        print(f"使用环境变量 FFMPEG_PATH: {ffmpeg_path}")
    else:
        # 环境变量无效或未设置，尝试 imageio_ffmpeg
        import imageio_ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        print(f"使用 imageio_ffmpeg 找到的 FFmpeg: {ffmpeg_path}")
except Exception as e:
    print(f"无法通过环境变量或 imageio_ffmpeg 获取 ffmpeg 路径 ({e})，将尝试使用系统路径中的 'ffmpeg'。")
    # 检查系统路径中的 ffmpeg 是否可用
    try:
        result = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore')
        print(f"'ffmpeg' 在系统路径中可用。版本信息:\n{result.stdout[:100]}...") # 打印部分版本信息
    except FileNotFoundError:
        print("错误: 在系统路径中也找不到 'ffmpeg'。视频输出（非 GIF/WebP）和音频合并将不可用。")
        print("请安装 ffmpeg 并确保它在系统 PATH 中，或者设置 FFMPEG_PATH 环境变量，或者安装 'pip install imageio[ffmpeg]'。")
        ffmpeg_path = None # 明确设置为 None
    except Exception as check_e:
         print(f"检查系统路径中的 'ffmpeg' 时出错: {check_e}")
         ffmpeg_path = None # 出错也设置为 None


# --- Helper Functions (Simplified from VHS) ---

def tensor_to_bytes(tensor):
    """Converts a torch tensor (H, W, C) to a numpy uint8 array."""
    if tensor.dim() > 3: # 处理可能的批次维度 B, H, W, C -> H, W, C
        tensor = tensor[0]
    # 确保数据在 CPU 上且为 float 类型以便乘法
    if tensor.is_cuda:
        tensor = tensor.cpu()
    if tensor.dtype != torch.float32 and tensor.dtype != torch.float64:
         # 尝试转换为 float32
         try:
             tensor = tensor.float()
         except Exception as e:
             print(f"警告: Tensor to float conversion failed: {e}. Trying direct numpy conversion.")

    # 转换为 NumPy 数组
    try:
        i = 255. * tensor.numpy()
    except TypeError as e:
         print(f"错误: Tensor to numpy conversion failed: {e}. Tensor dtype: {tensor.dtype}")
         # 提供一个默认的黑色像素数组以避免崩溃
         # 假设形状是 (H, W, C)，如果失败则用 (1,1,3)
         shape = tensor.shape if len(tensor.shape) == 3 else (1, 1, 3)
         return np.zeros(shape, dtype=np.uint8)

    img = np.clip(i, 0, 255).astype(np.uint8)
    return img

def to_pingpong(images_iterator):
    """Creates a ping-pong (forward and backward) sequence from an image iterator."""
    images_list = list(images_iterator) # 需要物化迭代器
    if len(images_list) > 1:
        # Exclude the first and last frames from the reversed part to avoid duplication
        return itertools.chain(images_list, reversed(images_list[1:-1]))
    else:
        return iter(images_list) # Return as iterator if only one frame

# --- Main Node Class ---

class Hua_Video_Output:
    def __init__(self):
        self.output_dir = OUTPUT_DIR
        self.temp_dir = TEMP_DIR
        self.type = "output"

    @classmethod
    def INPUT_TYPES(cls):
        # 添加更多编码器选项，包括 H.265 (HEVC)
        supported_formats = [
            "image/gif", "image/webp", "video/mp4", "video/webm", "video/mp4-hevc", "video/avi", "video/mkv"
        ]
        return {
            "required": {
                "images": ("IMAGE", {"tooltip": "The image frames to save as video/animated image."}),
                "filename_prefix": ("STRING", {"default": "ComfyUI_Video", "tooltip": "Prefix for the saved file."}),
                "frame_rate": ("FLOAT", {"default": 24.0, "min": 0, "step": 1}),
                "format": (supported_formats, {"default": "video/mp4", "tooltip": "Output format."}),
                "unique_id": ("STRING", {"default": "default_video_id", "multiline": False, "tooltip": "Unique ID for this execution provided by Gradio."}),
                "name": ("STRING", {"multiline": False, "default": "Hua_Video_Output", "tooltip": "节点名称"}),
            },
            "optional": {
                "loop_count": ("INT", {"default": 0, "min": 0, "max": 100, "step": 1, "tooltip": "Number of loops (0 for infinite loop). GIF/WebP only."}),
                "pingpong": ("BOOLEAN", {"default": False, "tooltip": "Enable ping-pong playback (forward then backward)."}),
                "save_output": ("BOOLEAN", {"default": True, "tooltip": "Save the file in the output directory (True) or temp directory (False)."}),
                "audio": ("AUDIO", {"tooltip": "Optional audio to combine with the video (requires ffmpeg)."}),
                "crf": ("INT", {"default": 23, "min": 0, "max": 51, "step": 1, "tooltip": "Constant Rate Factor (lower value means higher quality, larger file). For MP4/WebM/HEVC."}),
                "preset": (["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"], {"default": "fast", "tooltip": "Encoding preset (faster presets mean lower quality/compression). For MP4/HEVC."}),
                # "save_metadata_png": ("BOOLEAN", {"default": True, "tooltip": "Save a PNG file containing the metadata alongside the video/gif."}), # Removed based on user feedback
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    RETURN_TYPES = ()

    FUNCTION = "output_video_gradio"
    OUTPUT_NODE = True
    CATEGORY = icons.get("hua_boy_one")

    # 添加 preset 参数到函数签名
    def output_video_gradio(self, images, filename_prefix, frame_rate, format, unique_id,
                             loop_count=0, pingpong=False, save_output=True, audio=None, crf=23,
                             preset="fast", prompt=None, extra_pnginfo=None,name="Hua_Video_Output"): # Removed save_metadata_png parameter

        # 修复：检查输入是否为空，同时支持 Tensor 和 list，并计算 num_frames
        num_frames = 0
        if images is not None:
            if isinstance(images, torch.Tensor):
                num_frames = images.size(0)
            elif isinstance(images, list):
                # 确保列表中的元素是 Tensor
                if all(isinstance(img, torch.Tensor) for img in images):
                    num_frames = len(images)
                else:
                    print("错误: images 列表包含非 Tensor 元素。")
                    self._write_error_to_json(unique_id, "Input list contains non-Tensor elements.")
                    return ()
            else:
                print(f"警告: 未知的 images 类型: {type(images)}，视为空输入。")
                # num_frames 保持 0

        if num_frames == 0:
            print("错误: 没有图像帧输入。")
            self._write_error_to_json(unique_id, "No input frames.") # 恢复写入错误 JSON
            return self._create_error_result("No input frames.") # 同时返回 UI 错误

        # --- Determine Output Path ---
        output_dir = self.output_dir if save_output else self.temp_dir
        try:
            # 使用时间戳确保文件名唯一性，忽略 get_save_image_path 的计数器
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f") # 添加毫秒提高唯一性
            # subfolder = datetime.datetime.now().strftime('%Y-%m-%d') # 删除：不再按日期创建子文件夹
            full_output_folder = output_dir # 直接使用 output_dir
            # os.makedirs(full_output_folder, exist_ok=True) # 删除：output_dir 通常已存在
            filename_pattern = f"{filename_prefix}_{timestamp}" # 文件名主体

        except Exception as e:
            print(f"错误: 设置保存路径时出错: {e}")
            self._write_error_to_json(unique_id, f"Error setting save path: {e}") # 恢复写入错误 JSON
            return self._create_error_result(f"Error setting save path: {e}") # 同时返回 UI 错误

        # --- Prepare Frames ---
        try:
            # 迭代器方式处理帧，减少内存占用
            # num_frames 已在上面计算
            frames_iterator = (tensor_to_bytes(images[i]) for i in range(num_frames))

            # 需要获取第一帧来确定尺寸和检查 alpha
            first_frame_np = tensor_to_bytes(images[0])
            height, width, channels = first_frame_np.shape
            has_alpha = channels == 4
            print(f"视频帧尺寸: {width}x{height}, Alpha: {has_alpha}, 总帧数: {num_frames}")
        except Exception as e:
            print(f"错误: 准备图像帧时出错: {e}")
            self._write_error_to_json(unique_id, f"Error preparing image frames: {e}") # 恢复写入错误 JSON
            return self._create_error_result(f"Error preparing image frames: {e}") # 同时返回 UI 错误

        # --- Prepare Metadata (Similar to VHS) ---
        metadata = PngImagePlugin.PngInfo()
        video_metadata_dict = {} # 用于 ffmpeg -metadata
        if prompt is not None:
            try:
                prompt_str = json.dumps(prompt)
                metadata.add_text("prompt", prompt_str)
                video_metadata_dict["prompt"] = prompt_str # ffmpeg metadata value 不能太复杂
            except Exception as e:
                 print(f"警告: 无法序列化 prompt 到 metadata: {e}")
        if extra_pnginfo is not None:
            for k, v in extra_pnginfo.items():
                try:
                    # 尝试将值转换为字符串，避免复杂结构导致 ffmpeg 失败
                    v_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                    metadata.add_text(k, v_str) # PNG 支持更复杂的文本
                    # ffmpeg metadata key/value 限制更多
                    clean_k = re.sub(r'[^\w.-]', '_', str(k)) # 清理 key
                    clean_v = v_str.replace('=', '\\=').replace(';', '\\;').replace('#', '\\#').replace('\\', '\\\\').replace('\n', ' ') # 基本转义
                    if len(clean_k) > 0 and len(clean_v) < 256: # 简单长度限制
                        video_metadata_dict[clean_k] = clean_v
                except Exception as e:
                    print(f"警告: 无法序列化 extra_pnginfo '{k}' 到 metadata: {e}")
        metadata.add_text("CreationTime", datetime.datetime.now().isoformat(" ")[:19])
        video_metadata_dict["creation_time"] = datetime.datetime.now().isoformat(" ")[:19]


        # --- Save Metadata PNG (Optional) --- # Removed based on user feedback
        # png_filepath = None
        # if save_metadata_png:
        #     png_filename = f"{filename_pattern}_metadata.png"
        #     png_filepath = os.path.join(full_output_folder, png_filename)
        #     try:
        #         Image.fromarray(first_frame_np).save(
        #             png_filepath,
        #             pnginfo=metadata,
        #             compress_level=4,
        #         )
        #         print(f"元数据 PNG 已保存: {png_filepath}")
        #     except Exception as e:
        #         print(f"警告: 保存元数据 PNG 失败: {e}")
        #         png_filepath = None # 标记失败

        # --- Process Based on Format ---
        output_filepath = None
        final_paths_for_json = [] # 用于最终写入 JSON 的路径列表
        # if png_filepath: # 如果元数据 PNG 保存成功，加入列表 # Removed based on user feedback
        #      final_paths_for_json.append(png_filepath)

        format_type, format_ext = format.split("/")

        # --- Apply Pingpong if needed ---
        # Pingpong 需要在迭代器被消耗前应用
        if pingpong:
            print("应用 Pingpong...")
            # 需要重新创建迭代器，因为之前的可能已被消耗（获取第一帧）
            frames_list_for_pingpong = [tensor_to_bytes(images[i]) for i in range(num_frames)]
            if len(frames_list_for_pingpong) > 1:
                frames_iterator = itertools.chain(frames_list_for_pingpong, reversed(frames_list_for_pingpong[1:-1]))
                num_frames = len(frames_list_for_pingpong) + max(0, len(frames_list_for_pingpong) - 2) # 更新 pingpong 后的帧数
                print(f"Pingpong 应用后，有效帧数: {num_frames}")
            else:
                frames_iterator = iter(frames_list_for_pingpong) # 只有一帧，无需 pingpong
        else:
            # 如果没有 pingpong，重新创建原始迭代器
            frames_iterator = (tensor_to_bytes(images[i]) for i in range(num_frames))


        if format_type == "image" and format_ext in ["gif", "webp"]:
            # --- Save Animated Image using imageio ---
            ext = format_ext
            output_filename = f"{filename_pattern}.{ext}"
            output_filepath = os.path.join(full_output_folder, output_filename)

            try:
                # imageio 需要帧列表
                frames_list = list(frames_iterator)
                if not frames_list:
                     raise ValueError("帧列表为空，无法创建动画。")

                print(f"使用 imageio 保存 {ext.upper()} 到: {output_filepath}")
                # imageio 使用 'duration' (秒/帧), 而不是 fps
                duration_sec = 1.0 / frame_rate
                kwargs = {'duration': duration_sec, 'loop': loop_count}
                if ext == 'webp':
                    kwargs['lossless'] = True # 默认无损 WebP
                    kwargs['quality'] = 100 # 配合 lossless
                elif ext == 'gif':
                    kwargs['palettesize'] = 256
                    # kwargs['subrectangles'] = True # 优化 GIF 大小

                imageio.mimsave(output_filepath, frames_list, format=ext.upper(), **kwargs)
                print(f"{ext.upper()} 保存成功。")
                final_paths_for_json.append(output_filepath)

            except Exception as e:
                print(f"错误: 使用 imageio 保存 {ext.upper()} 失败: {e}")
                self._write_error_to_json(unique_id, f"Error saving {ext.upper()}: {e}", final_paths_for_json) # 恢复写入错误 JSON
                return self._create_error_result(f"Error saving {ext.upper()}: {e}", final_paths_for_json) # 同时返回 UI 错误

        elif format_type == "video" and ffmpeg_path:
            # --- Save Video using FFmpeg ---
            ext = format_ext # mp4, webm, etc.
            output_filename = f"{filename_pattern}.{ext}"
            output_filepath = os.path.join(full_output_folder, output_filename)
            temp_audio_path = None # 用于临时保存提取或转换的音频

            # --- Basic FFmpeg Command ---
            input_args = [
                "-f", "rawvideo",
                "-pix_fmt", "rgba" if has_alpha else "rgb24",
                "-s", f"{width}x{height}",
                "-r", str(frame_rate),
                "-i", "-", # 视频从 stdin 读取
            ]

            output_args = []
            # 通用视频编码设置
            # CRF 通常与特定编码器相关，在各自的 if/elif 块中设置更合适

            if ext == "mp4":
                output_args.extend(["-c:v", "libx264"])
                output_args.extend(["-crf", str(crf)]) # H.264 CRF
                # H.264 推荐的像素格式，处理 alpha (混合到黑色背景或丢弃)
                if has_alpha:
                    # 使用 alphaextract 和 overlay 将 alpha 混合到黑色背景
                    # 或者简单地使用 format=pix_fmts=yuv420p 丢弃 alpha
                    print("警告: MP4 不支持原生透明度。将使用 yuv420p (丢弃 Alpha)。")
                    output_args.extend(["-vf", "format=pix_fmts=yuv420p"])
                    output_args.extend(["-pix_fmt", "yuv420p"])
                else:
                    output_args.extend(["-pix_fmt", "yuv420p"]) # 标准 H.264 像素格式
                output_args.extend(["-preset", preset]) # 使用传入的 preset 参数
                output_args.extend(["-c:a", "aac", "-b:a", "192k"]) # 音频编码
            elif ext == "webm":
                output_args.extend(["-c:v", "libvpx-vp9"])
                output_args.extend(["-crf", str(crf)]) # VP9 CRF
                output_args.extend(["-b:v", "0"]) # VP9 CRF 模式需要 -b:v 0
                if has_alpha:
                    output_args.extend(["-pix_fmt", "yuva420p"]) # WebM 支持 alpha
                    output_args.extend(["-auto-alt-ref", "0"]) # 可能需要禁用以支持 alpha
                    print("WebM 格式，将保留 Alpha 通道。")
                else:
                    output_args.extend(["-pix_fmt", "yuv420p"])
                # VP9 没有标准的 preset，但有 -deadline (realtime, good, best) 和 -cpu-used
                # output_args.extend(["-deadline", "good", "-cpu-used", "1"]) # 示例：平衡质量和速度
                output_args.extend(["-c:a", "libopus", "-b:a", "128k"]) # 音频编码
            elif ext == "mp4-hevc":
                print("选择 HEVC (H.265) 编码器。")
                output_args.extend(["-c:v", "libx265"])
                output_args.extend(["-crf", str(crf)]) # H.265 CRF
                # 使用传入的 preset 参数
                output_args.extend(["-preset", preset]) # 使用传入的 preset 参数
                output_args.extend(["-tag:v", "hvc1"]) # 提高兼容性
                if has_alpha:
                    # HEVC 支持 Alpha 但需要特定像素格式如 yuv444p10le，且播放器支持有限
                    print("警告: HEVC (H.265) 对 Alpha 通道支持有限且可能不兼容。将使用 yuv420p (丢弃 Alpha)。")
                    output_args.extend(["-pix_fmt", "yuv420p"])
                else:
                    output_args.extend(["-pix_fmt", "yuv420p"])
                output_args.extend(["-c:a", "aac", "-b:a", "192k"]) # 音频编码
            else:
                # 对于其他未明确处理的 video/* 格式，回退到 H.264
                print(f"警告: 未知视频格式 '{format}'，将回退到 H.264 编码。")
                ext = "mp4" # 强制后缀为 mp4
                output_filename = f"{filename_pattern}.{ext}" # 更新文件名
                output_filepath = os.path.join(full_output_folder, output_filename) # 更新文件路径
                # 回退时也使用传入的 preset
                output_args.extend(["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"])
                output_args.extend(["-c:a", "aac", "-b:a", "192k"])


            # --- 处理音频 ---
            audio_input_args = []
            audio_map_args = []
            a_waveform = None
            sample_rate = None
            temp_audio_path = None # 确保在作用域开始时定义

            if audio is not None:
                print(f"接收到音频输入，类型: {type(audio)}") # 打印接收到的类型
                try:
                    # --- 修改点：尝试字典访问 ---
                    print("尝试使用字典访问 audio['waveform'] 和 audio['sample_rate']...")
                    # 检查 audio 是否是字典或类似字典的对象
                    if hasattr(audio, '__getitem__'):
                        a_waveform = audio['waveform']
                        sample_rate = audio['sample_rate']
                        print(f"成功通过字典访问提取 waveform (类型: {type(a_waveform)}) 和 sample_rate (值: {sample_rate})。")
                    else:
                         print(f"音频输入对象 (类型: {type(audio)}) 不支持字典访问。将忽略音频。")
                         a_waveform = None
                         sample_rate = None


                    # --- 通用音频数据验证和处理 ---
                    # 只有在成功获取 waveform 和 sample_rate 后才继续
                    if a_waveform is not None and sample_rate is not None:
                        if a_waveform.nelement() > 0: # 检查数据是否为空
                            print(f"验证有效音频: 采样率 {sample_rate}, 波形形状 {a_waveform.shape}, 数据类型: {a_waveform.dtype}")
                            channels = a_waveform.size(1)
                            # 准备临时音频文件
                            temp_audio_filename = f"temp_audio_{unique_id}.raw"
                            temp_audio_path = os.path.join(self.temp_dir, temp_audio_filename) # 在这里定义 temp_audio_path
                            print(f"准备写入临时音频文件: {temp_audio_path}")
                            try:
                                # waveform shape is (1, channels, samples), squeeze to (channels, samples)
                                # transpose to (samples, channels), then flatten and convert to bytes
                                # 确保在转换前数据在 CPU 上
                                waveform_cpu = a_waveform.squeeze(0).transpose(0, 1).contiguous().cpu()
                                # 检查数据类型，确保是 float32
                                if waveform_cpu.dtype != torch.float32:
                                    print(f"警告: 音频波形数据类型为 {waveform_cpu.dtype}，将尝试转换为 float32。")
                                    waveform_cpu = waveform_cpu.float()
                                audio_data_bytes = waveform_cpu.numpy().tobytes()
                                with open(temp_audio_path, 'wb') as f_audio:
                                    f_audio.write(audio_data_bytes)
                                print(f"临时音频文件写入成功，大小: {len(audio_data_bytes)} bytes")
                            except Exception as write_e:
                                print(f"错误: 写入临时音频文件失败: {write_e}")
                                temp_audio_path = None # 标记失败
                                a_waveform = None # 无法处理音频

                            # 只有在临时文件写入成功后才添加音频输入参数
                            if temp_audio_path and a_waveform is not None:
                                # 添加音频文件作为第二个输入
                                audio_input_args = [
                                    "-f", "f32le", # 输入格式 float32 little-endian
                                    "-ar", str(sample_rate),
                                    "-ac", str(channels),
                                    "-i", temp_audio_path,
                                ]
                                # 映射流: 0:v (视频), 1:a (音频)
                                audio_map_args = ["-map", "0:v", "-map", "1:a"]
                            # else: 如果写入失败，a_waveform 会被设为 None，下面会处理映射
                        else: # a_waveform.nelement() == 0
                            print("提供的音频数据波形为空，将忽略。")
                            a_waveform = None # 标记为无效
                    # else: a_waveform 或 sample_rate 为 None (提取失败或不支持字典访问)
                    # 不需要额外操作，因为 a_waveform 已经是 None

                # --- 修改点：捕获 KeyError/TypeError ---
                except (KeyError, TypeError) as e:
                    # 如果访问 audio['waveform'] 或 audio['sample_rate'] 失败
                    print(f"访问音频数据时出错 (类型: {type(audio)}): {e}。可能是缺少键或类型不兼容。将忽略音频。")
                    a_waveform = None
                    sample_rate = None
                except Exception as e:
                    # 捕获其他可能的异常 (例如数据转换失败)
                    print(f"处理音频数据时发生其他异常，将忽略音频: {e}")
                    a_waveform = None
                    sample_rate = None
                    # 清理可能已创建的临时文件
                    if temp_audio_path and os.path.exists(temp_audio_path):
                        try:
                            os.remove(temp_audio_path)
                            print(f"已清理部分写入的临时音频文件: {temp_audio_path}")
                        except OSError: pass
                    temp_audio_path = None # 确保路径无效

            # --- 决定最终的流映射 ---
            # 在所有音频处理逻辑之后，根据 a_waveform 和 temp_audio_path 的最终状态决定映射
            if a_waveform is not None and temp_audio_path is not None and os.path.exists(temp_audio_path):
                # 只有音频数据有效且临时文件成功写入，才进行音频映射
                # audio_map_args 已经在上面设置好了
                print("音频数据有效，将进行音视频流映射。")
            else:
                # 其他所有情况（无音频输入、类型错误、提取失败、写入失败、波形为空）都只映射视频
                audio_map_args = ["-map", "0:v"]
                if audio is not None: # 如果用户提供了音频但最终没用上
                    print("由于上述原因，音频将被忽略，仅输出视频流。")

            # --- 构建最终 FFmpeg 命令 ---
            command = [ffmpeg_path, "-y"] # -y 覆盖输出
            command.extend(input_args) # 视频输入来自 stdin

            # --- 诊断性修改：暂时禁用音频处理 ---
            print("诊断：暂时禁用音频处理以测试视频流稳定性。")
            # 强制不添加音频输入参数，即使它们已准备好
            # if audio_input_args:
            #     command.extend(audio_input_args) # 添加音频文件输入 (注释掉)
            command.extend(output_args) # 输出格式和编码
            command.extend(["-map", "0:v"]) # 强制只映射视频流

            # 添加元数据 (仍然有用)
            for k, v in video_metadata_dict.items():
                 command.extend(["-metadata", f"{k}={v}"])

            # 不添加 -shortest，因为没有音频
            # if a_waveform is not None:
            #     command.append("-shortest") # (注释掉)
            # --- 结束诊断性修改 ---

            command.append(output_filepath) # 输出文件路径

            # --- 增加日志：打印完整命令 ---
            full_command_str = ' '.join(command)
            print(f"准备执行 FFmpeg 命令: {full_command_str}")

            # --- 执行 FFmpeg ---
            process = None
            try:
                # 使用 stderr=subprocess.PIPE 来捕获错误信息
                # 设置 bufsize=0 尝试禁用缓冲，可能有助于实时获取输出，但主要还是 communicate() 获取最终结果
                print("启动 FFmpeg 子进程...")
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
                print(f"FFmpeg 进程已启动 (PID: {process.pid})。正在写入视频帧...") # 添加 PID

                # 向 stdin 写入视频帧数据
                frame_count = 0
                for frame_bytes in frames_iterator:
                    try:
                        process.stdin.write(frame_bytes.tobytes())
                        frame_count += 1
                    except (IOError, BrokenPipeError) as e:
                        print(f"警告: 向 ffmpeg stdin 写入时发生错误 (帧 {frame_count}, 可能已提前退出): {e}")
                        break # 停止写入

                print(f"已向 ffmpeg 写入 {frame_count} 帧。")

                # 移除显式的 stdin.close()，让 communicate() 处理
                print("所有帧已写入，准备调用 communicate() 等待 FFmpeg 完成...")

                # 等待进程完成并获取输出，增加超时（例如 300 秒 = 5 分钟）
                # communicate() 会负责关闭 stdin，读取 stdout/stderr，并等待进程结束
                timeout_seconds = 300
                try:
                    # input=None 因为数据已通过 stdin.write 写入
                    stdout, stderr = process.communicate(input=None, timeout=timeout_seconds)
                    return_code = process.returncode
                    print(f"FFmpeg communicate() 完成。返回码: {return_code}")
                except subprocess.TimeoutExpired:
                    print(f"错误: FFmpeg 在 {timeout_seconds} 秒内未完成，进程将被终止。")
                    process.terminate() # 尝试终止
                    # 再次尝试 communicate 以获取任何剩余输出（可能为空）
                    try:
                        stdout, stderr = process.communicate(timeout=1)
                    except subprocess.TimeoutExpired:
                        print("错误: 终止后 communicate 仍然超时。")
                        stdout, stderr = b"", b"" # 设为空字节串
                    except Exception as comm_err_after_term:
                        print(f"错误: 终止后 communicate 失败: {comm_err_after_term}")
                        stdout, stderr = b"", b""
                    return_code = -999 # 自定义超时错误码
                    # 记录错误
                    self._write_error_to_json(unique_id, f"FFmpeg timed out after {timeout_seconds} seconds.", final_paths_for_json) # 恢复写入错误 JSON
                    # 清理临时音频文件
                    if temp_audio_path and os.path.exists(temp_audio_path):
                        try: os.remove(temp_audio_path)
                        except OSError as e_clean: print(f"警告: 清理临时音频文件失败: {e_clean}")
                    return self._create_error_result(f"FFmpeg timed out after {timeout_seconds} seconds.", final_paths_for_json) # 同时返回 UI 错误

                # 解码 stdout 和 stderr
                stdout_str = stdout.decode('utf-8', errors='ignore')
                stderr_str = stderr.decode('utf-8', errors='ignore')

                print(f"FFmpeg 进程已结束，返回码: {return_code}")
                # 始终打印 stderr，因为它可能包含重要信息，即使返回码为 0
                print("--- FFmpeg stderr ---")
                print(stderr_str if stderr_str else "[无 stderr 输出]")
                print("---------------------")
                if stdout_str: # 通常 ffmpeg 不会将主要信息输出到 stdout
                    print("--- FFmpeg stdout ---")
                    print(stdout_str)
                    print("---------------------")


                if return_code == 0:
                    print(f"FFmpeg 执行成功，视频已保存到: {output_filepath}")
                    final_paths_for_json.append(output_filepath)
                    # 可以在这里添加额外的检查，例如文件大小 > 0
                    if not os.path.exists(output_filepath) or os.path.getsize(output_filepath) == 0:
                         print(f"警告: FFmpeg 返回码为 0，但输出文件 '{output_filepath}' 不存在或大小为 0。")
                         self._write_error_to_json(unique_id, f"FFmpeg reported success but output file is missing or empty.", final_paths_for_json) # 恢复写入错误 JSON
                         # 清理临时音频文件
                         if temp_audio_path and os.path.exists(temp_audio_path):
                             try: os.remove(temp_audio_path)
                             except OSError as e_clean: print(f"警告: 清理临时音频文件失败: {e_clean}")
                         return self._create_error_result(f"FFmpeg reported success but output file is missing or empty.", final_paths_for_json) # 同时返回 UI 错误
                else:
                    print(f"错误: FFmpeg 执行失败 (返回码: {return_code})")
                    # stderr 已在上面打印
                    self._write_error_to_json(unique_id, f"FFmpeg execution failed (code {return_code}). Check logs for details.", final_paths_for_json) # 恢复写入错误 JSON
                    # 清理临时音频文件
                    if temp_audio_path and os.path.exists(temp_audio_path):
                        try: os.remove(temp_audio_path)
                        except OSError as e_clean: print(f"警告: 清理临时音频文件失败: {e_clean}")
                    return self._create_error_result(f"FFmpeg execution failed (code {return_code}). Check logs for details.", final_paths_for_json) # 同时返回 UI 错误

            except Exception as e:
                print(f"错误: 执行 FFmpeg 时发生 Python 异常: {e}")
                if process and process.poll() is None:
                    process.terminate()
                self._write_error_to_json(unique_id, f"FFmpeg execution error: {e}", final_paths_for_json) # 恢复写入错误 JSON
                # 清理临时音频文件
                if temp_audio_path and os.path.exists(temp_audio_path):
                    try: os.remove(temp_audio_path)
                    except OSError as e_clean: print(f"警告: 清理临时音频文件失败: {e_clean}")
                return self._create_error_result(f"FFmpeg execution error: {e}", final_paths_for_json) # 同时返回 UI 错误
            finally:
                 # 确保最终清理临时音频文件
                 if temp_audio_path and os.path.exists(temp_audio_path):
                     try:
                         os.remove(temp_audio_path)
                         print(f"已清理临时音频文件: {temp_audio_path}")
                     except OSError as e_clean:
                         print(f"警告: 清理临时音频文件失败: {e_clean}")


        elif not ffmpeg_path and format_type == "video":
            print(f"错误: 需要 ffmpeg 来创建 {format} 视频，但未找到 ffmpeg。")
            self._write_error_to_json(unique_id, "FFmpeg not found, cannot create video format.", final_paths_for_json) # 恢复写入错误 JSON
            return self._create_error_result("FFmpeg not found, cannot create video format.", final_paths_for_json) # 同时返回 UI 错误
        else:
            print(f"错误: 不支持的格式 '{format}'。")
            self._write_error_to_json(unique_id, f"Unsupported format: {format}", final_paths_for_json) # 恢复写入错误 JSON
            return self._create_error_result(f"Unsupported format: {format}", final_paths_for_json) # 同时返回 UI 错误

        # --- 写入 JSON 并返回结果给前端 ---
        if output_filepath and os.path.exists(output_filepath):
            # --- 恢复写入 JSON 的逻辑 ---
            temp_json_path = os.path.join(self.temp_dir, f"{unique_id}.json")
            try:
                # 写入包含成功生成的文件列表的 JSON
                success_data = {"generated_files": final_paths_for_json}
                with open(temp_json_path, 'w', encoding='utf-8') as f:
                    json.dump(success_data, f, indent=4)
                print(f"最终文件路径列表已写入临时文件 (供 Gradio 使用): {temp_json_path}")
                print(f"路径列表: {final_paths_for_json}")

                # 验证文件是否存在 (可选，但建议保留)
                all_exist = True
                for path in final_paths_for_json:
                    if not os.path.exists(path):
                        print(f"错误: 最终文件不存在: {path}")
                        all_exist = False
                if not all_exist:
                     # 即使 JSON 写入成功，如果文件丢失也报告错误
                     self._write_error_to_json(unique_id, "One or more output files missing after generation.", final_paths_for_json) # 写入错误 JSON
                     return self._create_error_result("One or more output files missing after generation.", final_paths_for_json) # 返回 UI 错误

            except Exception as e:
                print(f"错误: 写入临时 JSON 文件失败 ({temp_json_path}): {e}")
                self._write_error_to_json(unique_id, f"Failed to write result JSON: {e}", final_paths_for_json) # 写入错误 JSON
                return self._create_error_result(f"Failed to write result JSON: {e}", final_paths_for_json) # 返回 UI 错误
            # --- 结束写入 JSON 的逻辑 ---

            # --- 准备返回给 ComfyUI 前端的结果 ---
            file_type = "output" if save_output else "temp"
            filename = os.path.basename(output_filepath)
            subfolder = "" # 相对于 type 目录

            print(f"准备返回给前端: filename={filename}, subfolder={subfolder}, type={file_type}")

            result = {
                "ui": {
                    "videos": [{
                        "filename": filename,
                        "subfolder": subfolder,
                        "type": file_type
                    }]
                }
            }
            return result
            # --- 结束返回给 ComfyUI 前端的结果 ---
        else:
            # 如果最终没有有效的 output_filepath，写入错误 JSON 并返回错误结果
            print("最终未生成有效输出文件，写入错误 JSON 并返回错误结果。")
            error_msg = "Failed to generate output file."
            if not final_paths_for_json:
                error_msg = "No valid output files generated (check previous errors)."
            elif output_filepath and not os.path.exists(output_filepath): # 检查 output_filepath 是否已定义
                 error_msg = f"Output file missing after generation: {output_filepath}"

            self._write_error_to_json(unique_id, error_msg, final_paths_for_json) # 写入错误 JSON
            return self._create_error_result(error_msg, final_paths_for_json) # 返回 UI 错误


    # 恢复 _write_error_to_json 函数
    def _write_error_to_json(self, unique_id, error_message, existing_paths=None):
        """Helper to write an error structure to the JSON file for Gradio."""
        if existing_paths is None:
            existing_paths = []
        temp_json_path = os.path.join(self.temp_dir, f"{unique_id}.json")
        error_data = {
            "error": error_message,
            "generated_files": existing_paths # 包含任何已成功生成的文件
        }
        try:
            with open(temp_json_path, 'w', encoding='utf-8') as f:
                json.dump(error_data, f, indent=4)
            print(f"错误信息已写入临时文件 (供 Gradio 使用): {temp_json_path}")
        except Exception as e:
            print(f"严重错误: 连错误信息都无法写入 JSON 文件 ({temp_json_path}): {e}")

    # 保留 _create_error_result 函数用于返回 UI 错误
    def _create_error_result(self, error_message, existing_paths=None):
        """Helper to create an error structure for the ComfyUI UI."""
        if existing_paths is None:
            existing_paths = []
        print(f"创建 UI 错误结果: {error_message}")
        # 返回一个包含错误信息的字典，前端 onExecuted 可以检查这个
        # 也可以包含任何已成功生成的文件，供参考
        return {
            "ui": {
                "error": [error_message], # 使用列表，即使只有一个错误
                "generated_files": existing_paths # 包含任何已成功生成的文件
            }
        }
