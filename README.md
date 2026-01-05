已经支持最新gradio版本依赖，
`gradio_workflow.py` 目前支持 __4__ 种动态组件类：
1. __`GradioTextOk`__: 用于动态生成正向提示词输入框 (`gr.Textbox`)。
2. __`Hua_LoraLoaderModelOnly`__: 用于动态生成 Lora 模型选择下拉框 (`gr.Dropdown`)。
3. __`HuaIntNode`__: 用于动态生成整数输入框 (`gr.Number`)。
4. __`HuaFloatNode`__: 用于动态生成浮点数输入框 (`gr.Number`)
![newui2](https://github.com/kungful/ComfyUI_to_webui/blob/bf6a409f1c664e65e7fe6b1012809617c547260a/Sample_preview/8888.png)
你可以在我的镜像中直接使用该插件
[免费镜像](https://www.xiangongyun.com/image/detail/7b36c1a3-da41-4676-b5b3-03ec25d6e197)
![前后端原理](https://github.com/kungful/ComfyUI_to_webui/blob/9b57fff3c120bcb09d265ac3e75e4e8c04e84015/Sample_preview/%E5%89%8D%E5%90%8E%E7%AB%AF%E5%AF%B9%E6%8E%A5%E5%8E%9F%E7%90%86.png)
## 概述
<span style="color:blue;">**示例工作流在**</span> Sample_preview 文件夹里面
<span style="color:blue;">**`ComfyUI_to_webui` 是一个为 ComfyUI工作流变成webui的项目**</span>

## 计划的功能
- **横向标签设置功能**: 已经完成编写.......
- **节点支持多国语言**: 已经编写完成
- **根据节点数量动态gradio组件增加**:  准备中..........
- **收录到comfyui管理器**: 已经拉取请求
- **实时日志预览**: 已经编写完成
- **队列功能**:已完成编写
- **多图显示，预览所有图片**:已完成编写
- **自动保存api json流**: 已编写完成
- **gradio前端动态显示图像输入口**：已编写完成
- **模型选择**：已经编写完成
- **分辨率选择**： 已经编写完成
- **种子管理**：已编写完成
- **生成的批次** 开发中.....
  <span style="color:purple;">随机种已经完成</span>
- **增强的界面**：已经优化

## 安装
如果comfyui加载时自动安装模块没能成功可用以下方法手动安装
### 导航到custom_nodes
1. **克隆仓库**：
   ```bash
   git clone https://github.com/kungful/ComfyUI_to_webui.git
   cd ComfyUI_to_webui
   ..\..\..\python_embeded\python.exe -m pip install -r requirements.txt
## 使用方法
你的comfyui搭建好工作流后不需要手动保存api格式json文件，只需要运行一遍跑通后就可以了，在输出端接入"☀️gradio前端传入图像这个节点就行

### 已经完成自动保存api工作流功能，工作流位置在output
1. **api工作流自动保存位置**
   ```bash
   D:\
     └── comfyUI\
       ├── ComfyUI\
       │   ├── output
       │   └── ...
     
### 注意一个问题
建议接入 （🧙hua_gradio随机种）这个节点，不然comfyui识别到相同的json数据不进行推理直接死机.有了随机种的变化就没问题了。

### 思维导图节点
不可推理
![预览image](https://github.com/kungful/ComfyUI_to_webui/blob/4af4203a114cef054bf31287f1f191fa8b0f5742/Sample_preview/6b8564af2dbb2b75185f0bcc7cf5cd5.png)

### 这是检索多提示词字符串判断图片是否传递哪个模型和图片的布尔节点，为的是跳过puilid的报错
![预览image](https://github.com/kungful/ComfyUI_to_webui/blob/4af4203a114cef054bf31287f1f191fa8b0f5742/Sample_preview/image.png)
![预览model](https://github.com/kungful/ComfyUI_to_webui/blob/4af4203a114cef054bf31287f1f191fa8b0f5742/Sample_preview/model.png)
![image](https://github.com/user-attachments/assets/85867dab-ded0-46f3-b0f7-a1e3e0843600)


