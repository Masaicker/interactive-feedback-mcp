# Interactive Feedback MCP UI
# Developed by Fábio Ferreira (https://x.com/fabiomlferreira)
# Inspired by/related to dotcursorrules.com (https://dotcursorrules.com/)
import os
import sys
import json
import psutil
import argparse
import subprocess
import threading
import hashlib
import base64
import io
from typing import Optional, TypedDict
from urllib.parse import unquote

import requests
from PIL import Image
import google.generativeai as genai

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QTextEdit, QGroupBox,
    QComboBox, QFileDialog, QScrollArea, QFrame
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QSettings, QThread
from PySide6.QtGui import QTextCursor, QIcon, QKeyEvent, QFont, QFontDatabase, QPalette, QColor, QPixmap

class FeedbackResult(TypedDict):
    command_logs: str
    interactive_feedback: str

class FeedbackConfig(TypedDict):
    run_command: str
    execute_automatically: bool
    gemini_api_key: str
    gemini_model: str
    gemini_proxy: str

def set_dark_title_bar(widget: QWidget, dark_title_bar: bool) -> None:
    # Ensure we're on Windows
    if sys.platform != "win32":
        return

    from ctypes import windll, c_uint32, byref

    # Get Windows build number
    build_number = sys.getwindowsversion().build
    if build_number < 17763:  # Windows 10 1809 minimum
        return

    # Check if the widget's property already matches the setting
    dark_prop = widget.property("DarkTitleBar")
    if dark_prop is not None and dark_prop == dark_title_bar:
        return

    # Set the property (True if dark_title_bar != 0, False otherwise)
    widget.setProperty("DarkTitleBar", dark_title_bar)

    # Load dwmapi.dll and call DwmSetWindowAttribute
    dwmapi = windll.dwmapi
    hwnd = widget.winId()  # Get the window handle
    attribute = 20 if build_number >= 18985 else 19  # Use newer attribute for newer builds
    c_dark_title_bar = c_uint32(dark_title_bar)  # Convert to C-compatible uint32
    dwmapi.DwmSetWindowAttribute(hwnd, attribute, byref(c_dark_title_bar), 4)

    # HACK: Create a 1x1 pixel frameless window to force redraw
    temp_widget = QWidget(None, Qt.FramelessWindowHint)
    temp_widget.resize(1, 1)
    temp_widget.move(widget.pos())
    temp_widget.show()
    temp_widget.deleteLater()  # Safe deletion in Qt event loop

def get_dark_mode_palette(app: QApplication):
    darkPalette = app.palette()
    darkPalette.setColor(QPalette.Window, QColor(53, 53, 53))
    darkPalette.setColor(QPalette.WindowText, Qt.white)
    darkPalette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(127, 127, 127))
    darkPalette.setColor(QPalette.Base, QColor(42, 42, 42))
    darkPalette.setColor(QPalette.AlternateBase, QColor(66, 66, 66))
    darkPalette.setColor(QPalette.ToolTipBase, QColor(53, 53, 53))
    darkPalette.setColor(QPalette.ToolTipText, Qt.white)
    darkPalette.setColor(QPalette.Text, Qt.white)
    darkPalette.setColor(QPalette.Disabled, QPalette.Text, QColor(127, 127, 127))
    darkPalette.setColor(QPalette.Dark, QColor(35, 35, 35))
    darkPalette.setColor(QPalette.Shadow, QColor(20, 20, 20))
    darkPalette.setColor(QPalette.Button, QColor(53, 53, 53))
    darkPalette.setColor(QPalette.ButtonText, Qt.white)
    darkPalette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(127, 127, 127))
    darkPalette.setColor(QPalette.BrightText, Qt.red)
    darkPalette.setColor(QPalette.Link, QColor(42, 130, 218))
    darkPalette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    darkPalette.setColor(QPalette.Disabled, QPalette.Highlight, QColor(80, 80, 80))
    darkPalette.setColor(QPalette.HighlightedText, Qt.white)
    darkPalette.setColor(QPalette.Disabled, QPalette.HighlightedText, QColor(127, 127, 127))
    darkPalette.setColor(QPalette.PlaceholderText, QColor(127, 127, 127))
    return darkPalette

def kill_tree(process: subprocess.Popen):
    killed: list[psutil.Process] = []
    parent = psutil.Process(process.pid)
    for proc in parent.children(recursive=True):
        try:
            proc.kill()
            killed.append(proc)
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass
    killed.append(parent)

    # Terminate any remaining processes
    for proc in killed:
        try:
            if proc.is_running():
                proc.terminate()
        except psutil.Error:
            pass

def get_user_environment() -> dict[str, str]:
    if sys.platform != "win32":
        return os.environ.copy()

    import ctypes
    from ctypes import wintypes

    # Load required DLLs
    advapi32 = ctypes.WinDLL("advapi32")
    userenv = ctypes.WinDLL("userenv")
    kernel32 = ctypes.WinDLL("kernel32")

    # Constants
    TOKEN_QUERY = 0x0008

    # Function prototypes
    OpenProcessToken = advapi32.OpenProcessToken
    OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    OpenProcessToken.restype = wintypes.BOOL

    CreateEnvironmentBlock = userenv.CreateEnvironmentBlock
    CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    CreateEnvironmentBlock.restype = wintypes.BOOL

    DestroyEnvironmentBlock = userenv.DestroyEnvironmentBlock
    DestroyEnvironmentBlock.argtypes = [wintypes.LPVOID]
    DestroyEnvironmentBlock.restype = wintypes.BOOL

    GetCurrentProcess = kernel32.GetCurrentProcess
    GetCurrentProcess.argtypes = []
    GetCurrentProcess.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    # Get process token
    token = wintypes.HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise RuntimeError("Failed to open process token")

    try:
        # Create environment block
        environment = ctypes.c_void_p()
        if not CreateEnvironmentBlock(ctypes.byref(environment), token, False):
            raise RuntimeError("Failed to create environment block")

        try:
            # Convert environment block to list of strings
            result = {}
            env_ptr = ctypes.cast(environment, ctypes.POINTER(ctypes.c_wchar))
            offset = 0

            while True:
                # Get string at current offset
                current_string = ""
                while env_ptr[offset] != "\0":
                    current_string += env_ptr[offset]
                    offset += 1

                # Skip null terminator
                offset += 1

                # Break if we hit double null terminator
                if not current_string:
                    break

                equal_index = current_string.index("=")
                if equal_index == -1:
                    continue

                key = current_string[:equal_index]
                value = current_string[equal_index + 1:]
                result[key] = value

            return result

        finally:
            DestroyEnvironmentBlock(environment)

    finally:
        CloseHandle(token)

class FeedbackTextEdit(QTextEdit):
    image_pasted = Signal(bytes)  # 新增信号，用于通知图片粘贴
    
    def __init__(self, parent=None):
        super().__init__(parent)

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Return and event.modifiers() == Qt.ControlModifier:
            # Find the parent FeedbackUI instance and call submit
            parent = self.parent()
            while parent and not isinstance(parent, FeedbackUI):
                parent = parent.parent()
            if parent:
                parent._submit_feedback()
        else:
            super().keyPressEvent(event)
    
    def insertFromMimeData(self, source):
        # 检查是否有图片数据
        if source.hasImage():
            try:
                # 获取图片数据
                image = source.imageData()
                if image and not image.isNull():
                    # 转换为字节数据
                    pixmap = QPixmap.fromImage(image)
                    buffer = io.BytesIO()
                    # 使用更安全的保存方法
                    success = pixmap.save(buffer, "PNG")
                    if success:
                        image_bytes = buffer.getvalue()
                        buffer.close()
                        
                        # 发送信号通知有图片粘贴
                        self.image_pasted.emit(image_bytes)
                        
                        # 静默处理，不显示提示信息
                        return
                    else:
                        # 如果保存失败，静默处理
                        print("图片保存失败")
                        return
            except Exception as e:
                # 捕获所有异常，避免崩溃
                print(f"处理粘贴图片时出错: {e}")
                return
        
        # 只插入纯文本，忽略其他格式
        if source.hasText():
            plain_text = source.text()
            # 插入纯文本到当前光标位置
            cursor = self.textCursor()
            cursor.insertText(plain_text)
        else:
            # 如果没有文本数据，使用默认行为
            super().insertFromMimeData(source)

class DragDropImageLabel(QLabel):
    """支持拖拽和粘贴的图片预览标签"""
    image_dropped = Signal(bytes)  # 新增信号，用于通知图片被拖拽进来
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.StrongFocus)  # 允许获取焦点
        self.setText("支持拖拽图片到此处或使用Ctrl+V粘贴")
        self.setStyleSheet("border: 2px dashed #aaa; padding: 20px;")
        self.setAlignment(Qt.AlignCenter)
        
    def dragEnterEvent(self, event):
        """拖拽进入事件"""
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
            
    def dragMoveEvent(self, event):
        """拖拽移动事件"""
        if event.mimeData().hasImage() or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
            
    def dropEvent(self, event):
        """拖拽放下事件"""
        mime_data = event.mimeData()
        
        try:
            # 处理图片数据
            if mime_data.hasImage():
                image = mime_data.imageData()
                if image and not image.isNull():
                    pixmap = QPixmap.fromImage(image)
                    buffer = io.BytesIO()
                    success = pixmap.save(buffer, "PNG")
                    if success:
                        image_bytes = buffer.getvalue()
                        buffer.close()
                        self.image_dropped.emit(image_bytes)
                        event.acceptProposedAction()
                        return
                        
            # 处理文件URL
            elif mime_data.hasUrls():
                urls = mime_data.urls()
                if urls:
                    file_path = urls[0].toLocalFile()
                    if file_path and file_path.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')):
                        try:
                            with open(file_path, 'rb') as f:
                                image_data = f.read()
                            self.image_dropped.emit(image_data)
                            event.acceptProposedAction()
                            return
                        except Exception as e:
                            print(f"读取拖拽文件失败: {e}")
                            
        except Exception as e:
            print(f"处理拖拽事件失败: {e}")
            
        event.ignore()
        
    def keyPressEvent(self, event):
        """处理键盘事件，特别是Ctrl+V"""
        if event.matches(QKeyEvent.Paste):
            clipboard = QApplication.clipboard()
            mime_data = clipboard.mimeData()
            
            if mime_data.hasImage():
                try:
                    image = mime_data.imageData()
                    if image and not image.isNull():
                        pixmap = QPixmap.fromImage(image)
                        buffer = io.BytesIO()
                        success = pixmap.save(buffer, "PNG")
                        if success:
                            image_bytes = buffer.getvalue()
                            buffer.close()
                            self.image_dropped.emit(image_bytes)
                            return
                except Exception as e:
                    print(f"处理粘贴图片失败: {e}")
                    
        super().keyPressEvent(event)

class LogSignals(QObject):
    append_log = Signal(str)

class GeminiWorker(QThread):
    finished = Signal(str)  # 返回分析结果
    error = Signal(str)     # 返回错误信息
    
    def __init__(self, api_key: str, model: str, proxy: str, text: str, image_data: bytes = None):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self.proxy = proxy
        self.text = text
        self.image_data = image_data
    
    def run(self):
        try:
            # 设置代理（仅对Gemini API）
            if self.proxy:
                os.environ['HTTP_PROXY'] = self.proxy
                os.environ['HTTPS_PROXY'] = self.proxy
            
            # 配置Gemini API
            genai.configure(api_key=self.api_key)
            
            # 获取模型
            model = genai.GenerativeModel(self.model)
            
            # 构建请求内容
            contents = []
            
            # 添加文本
            if self.text:
                contents.append(self.text)
            
            # 添加图片（如果有）
            if self.image_data:
                # 将图片数据转换为PIL Image
                image = Image.open(io.BytesIO(self.image_data))
                contents.append(image)
            
            # 调用Gemini API
            response = model.generate_content(contents)
            
            self.finished.emit(response.text)
            
        except Exception as e:
            self.error.emit(f"Gemini API调用失败: {str(e)}")
        finally:
            # 清理代理设置
            if self.proxy:
                if 'HTTP_PROXY' in os.environ:
                    del os.environ['HTTP_PROXY']
                if 'HTTPS_PROXY' in os.environ:
                    del os.environ['HTTPS_PROXY']

class FeedbackUI(QMainWindow):
    def __init__(self, project_directory: str, prompt: str):
        super().__init__()
        
        # URL decode the project directory path
        decoded_path = unquote(project_directory)
        
        # Convert Unix-style paths to Windows format on Windows
        if sys.platform == "win32" and decoded_path.startswith('/') and len(decoded_path) > 2:
            if decoded_path[2] == ':':  # Handle /d:/path format
                decoded_path = decoded_path[1:]
            elif decoded_path.startswith('/d%3A/'):  # Handle URL-encoded /d:/path format
                decoded_path = decoded_path.replace('/d%3A/', 'D:/')
            elif '%3A' in decoded_path:  # Handle other URL-encoded drive paths
                decoded_path = decoded_path.replace('%3A', ':').lstrip('/')
        
        self.project_directory = os.path.abspath(decoded_path)
        
        # Ensure the path exists
        if not os.path.exists(self.project_directory):
            raise ValueError(f"项目目录不存在: {self.project_directory}")
        
        self.prompt = prompt

        self.process: Optional[subprocess.Popen] = None
        self.log_buffer = []
        self.feedback_result = None
        self.log_signals = LogSignals()
        self.log_signals.append_log.connect(self._append_log)

        self.setWindowTitle("Interactive Feedback MCP")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "images", "feedback.png")
        self.setWindowIcon(QIcon(icon_path))
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        
        # Set application-wide font size
        app_font = QApplication.instance().font()
        app_font.setPointSize(11)  # Increased from default to 11pt
        QApplication.instance().setFont(app_font)
        
        self.settings = QSettings("InteractiveFeedbackMCP", "InteractiveFeedbackMCP")
        
        # Load general UI settings for the main window (geometry, state)
        self.settings.beginGroup("MainWindow_General")
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            self.resize(900, 700)  # Increased size to accommodate larger fonts
            screen = QApplication.primaryScreen().geometry()
            x = (screen.width() - 900) // 2
            y = (screen.height() - 700) // 2
            self.move(x, y)
        state = self.settings.value("windowState")
        if state:
            self.restoreState(state)
        self.settings.endGroup() # End "MainWindow_General" group
        
        # Load project-specific settings (command, auto-execute, command section visibility)
        self.project_group_name = get_project_settings_group(self.project_directory)
        self.settings.beginGroup(self.project_group_name)
        loaded_run_command = self.settings.value("run_command", "", type=str)
        loaded_execute_auto = self.settings.value("execute_automatically", False, type=bool)
        command_section_visible = self.settings.value("commandSectionVisible", False, type=bool)
        loaded_gemini_api_key = self.settings.value("gemini_api_key", "", type=str)
        loaded_gemini_model = self.settings.value("gemini_model", "gemini-1.5-flash-latest", type=str)
        loaded_gemini_proxy = self.settings.value("gemini_proxy", "", type=str)
        self.settings.endGroup() # End project-specific group
        
        self.config: FeedbackConfig = {
            "run_command": loaded_run_command,
            "execute_automatically": loaded_execute_auto,
            "gemini_api_key": loaded_gemini_api_key,
            "gemini_model": loaded_gemini_model,
            "gemini_proxy": loaded_gemini_proxy
        }
        
        # 图片数据存储
        self.current_image_data = None
        self.gemini_worker = None

        self._create_ui() # self.config is used here to set initial values

        # Set command section visibility AFTER _create_ui has created relevant widgets
        self.command_group.setVisible(command_section_visible)
        if command_section_visible:
            self.toggle_command_button.setText("隐藏命令区域")
        else:
            self.toggle_command_button.setText("显示命令区域")

        set_dark_title_bar(self, True)

        if self.config.get("execute_automatically", False):
            self._run_command()

    def _format_windows_path(self, path: str) -> str:
        if sys.platform == "win32":
            # Convert forward slashes to backslashes
            path = path.replace("/", "\\")
            # Capitalize drive letter if path starts with x:\
            if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
                path = path[0].upper() + path[1:]
        return path

    def _create_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Toggle Command Section Button
        self.toggle_command_button = QPushButton("显示命令区域")
        self.toggle_command_button.clicked.connect(self._toggle_command_section)
        layout.addWidget(self.toggle_command_button)

        # Command section
        self.command_group = QGroupBox("命令")
        command_layout = QVBoxLayout(self.command_group)

        # Working directory label
        formatted_path = self._format_windows_path(self.project_directory)
        working_dir_label = QLabel(f"工作目录: {formatted_path}")
        command_layout.addWidget(working_dir_label)

        # Command input row
        command_input_layout = QHBoxLayout()
        self.command_entry = QLineEdit()
        self.command_entry.setText(self.config["run_command"])
        self.command_entry.returnPressed.connect(self._run_command)
        self.command_entry.textChanged.connect(self._update_config)
        self.run_button = QPushButton("运行(&R)")
        self.run_button.clicked.connect(self._run_command)

        command_input_layout.addWidget(self.command_entry)
        command_input_layout.addWidget(self.run_button)
        command_layout.addLayout(command_input_layout)

        # Auto-execute and save config row
        auto_layout = QHBoxLayout()
        self.auto_check = QCheckBox("下次运行时自动执行")
        self.auto_check.setChecked(self.config.get("execute_automatically", False))
        self.auto_check.stateChanged.connect(self._update_config)

        save_button = QPushButton("保存配置(&S)")
        save_button.clicked.connect(self._save_config)

        auto_layout.addWidget(self.auto_check)
        auto_layout.addStretch()
        auto_layout.addWidget(save_button)
        command_layout.addLayout(auto_layout)

        # Console section (now part of command_group)
        console_group = QGroupBox("控制台")
        console_layout_internal = QVBoxLayout(console_group)
        console_group.setMinimumHeight(250)  # Increased height

        # Log text area
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        font = QFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        font.setPointSize(12)  # Increased from 9 to 12pt
        self.log_text.setFont(font)
        console_layout_internal.addWidget(self.log_text)

        # Clear button
        button_layout = QHBoxLayout()
        self.clear_button = QPushButton("清除(&C)")
        self.clear_button.clicked.connect(self.clear_logs)
        button_layout.addStretch()
        button_layout.addWidget(self.clear_button)
        console_layout_internal.addLayout(button_layout)
        
        # 图片识别配置区域（移到命令区域）
        image_config_group = QGroupBox("图片识别配置")
        image_config_layout = QVBoxLayout(image_config_group)
        
        # API Key 输入
        api_key_layout = QHBoxLayout()
        api_key_label = QLabel("Gemini API Key:")
        self.api_key_entry = QLineEdit()
        self.api_key_entry.setEchoMode(QLineEdit.Password)
        self.api_key_entry.setText(self.config["gemini_api_key"])
        self.api_key_entry.textChanged.connect(self._update_gemini_config)
        self.api_key_entry.setPlaceholderText("请输入您的Gemini API Key")
        api_key_layout.addWidget(api_key_label)
        api_key_layout.addWidget(self.api_key_entry)
        image_config_layout.addLayout(api_key_layout)
        
        # 模型选择和代理设置
        model_proxy_layout = QHBoxLayout()
        
        # 模型选择
        model_label = QLabel("模型:")
        self.model_combo = QComboBox()
        self.model_combo.addItems([
            "gemini-2.5-flash-preview-05-20",
            "gemini-2.5-pro-preview-05-06",
            "gemini-2.0-flash-exp",
            "gemini-2.0-flash-thinking-exp",
            "gemini-2.0-flash-thinking-exp-1219",
            "gemini-1.5-pro-latest",
            "gemini-1.5-flash-latest",
            "gemini-1.5-pro-exp-0827",
            "gemini-1.5-flash-exp-0827",
            "gemini-1.5-flash-8b-exp-0924",
            "gemini-pro"
        ])
        self.model_combo.setCurrentText(self.config["gemini_model"])
        self.model_combo.currentTextChanged.connect(self._update_gemini_config)
        
        # 代理设置
        proxy_label = QLabel("代理:")
        self.proxy_entry = QLineEdit()
        self.proxy_entry.setText(self.config["gemini_proxy"])
        self.proxy_entry.textChanged.connect(self._update_gemini_config)
        self.proxy_entry.setPlaceholderText("http://proxy:port (可选)")
        
        model_proxy_layout.addWidget(model_label)
        model_proxy_layout.addWidget(self.model_combo)
        model_proxy_layout.addWidget(proxy_label)
        model_proxy_layout.addWidget(self.proxy_entry)
        image_config_layout.addLayout(model_proxy_layout)
        
        command_layout.addWidget(image_config_group)
        command_layout.addWidget(console_group)

        self.command_group.setVisible(False) 
        layout.addWidget(self.command_group)

        # Feedback section with adjusted height
        self.feedback_group = QGroupBox("反馈")
        feedback_layout = QVBoxLayout(self.feedback_group)

        # Short description label (from self.prompt)
        self.description_label = QLabel(self.prompt)
        self.description_label.setWordWrap(True)
        # Increase font size for description
        desc_font = self.description_label.font()
        desc_font.setPointSize(12)
        self.description_label.setFont(desc_font)
        feedback_layout.addWidget(self.description_label)
        
        # 图片区域
        image_group = QGroupBox("图片")
        image_layout = QVBoxLayout(image_group)
        
        # 图片操作按钮
        image_buttons_layout = QHBoxLayout()
        self.upload_image_button = QPushButton("上传图片")
        self.upload_image_button.clicked.connect(self._upload_image)
        self.clear_image_button = QPushButton("清除图片")
        self.clear_image_button.clicked.connect(self._clear_image)
        self.clear_image_button.setEnabled(False)
        
        self.analyze_image_button = QPushButton("图片识别")
        self.analyze_image_button.clicked.connect(self._analyze_image)
        self.analyze_image_button.setEnabled(False)  # 初始状态禁用
        
        image_buttons_layout.addWidget(self.upload_image_button)
        image_buttons_layout.addWidget(self.clear_image_button)
        image_buttons_layout.addStretch()
        image_buttons_layout.addWidget(self.analyze_image_button)
        image_layout.addLayout(image_buttons_layout)
        
        # 图片预览区域
        self.image_preview_scroll = QScrollArea()
        self.image_preview_scroll.setMaximumHeight(200)
        self.image_preview_scroll.setWidgetResizable(True)
        self.image_preview_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.image_preview_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        
        self.image_preview_label = DragDropImageLabel()
        self.image_preview_label.image_dropped.connect(self._handle_image_paste)
        
        self.image_preview_scroll.setWidget(self.image_preview_label)
        image_layout.addWidget(self.image_preview_scroll)
        
        # 图片分析提示输入框
        analysis_prompt_layout = QHBoxLayout()
        analysis_prompt_label = QLabel("分析提示:")
        self.analysis_prompt_entry = QLineEdit()
        self.analysis_prompt_entry.setPlaceholderText("请输入您希望AI分析图片的具体要求，例如：请详细描述这张图片的内容")
        # 默认为空，点击时会自动使用默认提示
        
        analysis_prompt_layout.addWidget(analysis_prompt_label)
        analysis_prompt_layout.addWidget(self.analysis_prompt_entry)
        image_layout.addLayout(analysis_prompt_layout)
        
        feedback_layout.addWidget(image_group)

        self.feedback_text = FeedbackTextEdit()
        # Set larger font for feedback text
        feedback_font = QFont()
        feedback_font.setPointSize(12)
        self.feedback_text.setFont(feedback_font)
        
        font_metrics = self.feedback_text.fontMetrics()
        row_height = font_metrics.height()
        # Calculate height for 5 lines + some padding for margins
        padding = self.feedback_text.contentsMargins().top() + self.feedback_text.contentsMargins().bottom() + 5 # 5 is extra vertical padding
        self.feedback_text.setMinimumHeight(6 * row_height + padding)  # Increased to 6 lines

        self.feedback_text.setPlaceholderText("请在此输入您的反馈 (Ctrl+Enter 提交)")
        
        # 连接图片粘贴信号
        self.feedback_text.image_pasted.connect(self._handle_image_paste)
        
        # 按钮布局
        button_layout = QHBoxLayout()
        submit_button = QPushButton("发送反馈(&F) (Ctrl+Enter)")
        submit_button.clicked.connect(self._submit_feedback)
        
        button_layout.addWidget(submit_button)
        button_layout.addStretch()

        feedback_layout.addWidget(self.feedback_text)
        feedback_layout.addLayout(button_layout)

        # Set minimum height for feedback_group to accommodate its contents
        # This will be based on the description label and the 6-line feedback_text
        self.feedback_group.setMinimumHeight(self.description_label.sizeHint().height() + self.feedback_text.minimumHeight() + submit_button.sizeHint().height() + feedback_layout.spacing() * 2 + feedback_layout.contentsMargins().top() + feedback_layout.contentsMargins().bottom() + 10) # 10 for extra padding

        # Add widgets in a specific order
        layout.addWidget(self.feedback_group)

        # Credits/Contact Label
        contact_label = QLabel('需要改进？请联系 Fábio Ferreira <a href="https://x.com/fabiomlferreira">X.com</a> 或访问 <a href="https://dotcursorrules.com/">dotcursorrules.com</a>')
        contact_label.setOpenExternalLinks(True)
        contact_label.setAlignment(Qt.AlignCenter)
        contact_label.setStyleSheet("font-size: 11pt; color: #cccccc;") # Increased from 9pt to 11pt
        layout.addWidget(contact_label)

    def _toggle_command_section(self):
        is_visible = self.command_group.isVisible()
        self.command_group.setVisible(not is_visible)
        if not is_visible:
            self.toggle_command_button.setText("隐藏命令区域")
        else:
            self.toggle_command_button.setText("显示命令区域")
        
        # Immediately save the visibility state for this project
        self.settings.beginGroup(self.project_group_name)
        self.settings.setValue("commandSectionVisible", self.command_group.isVisible())
        self.settings.endGroup()

        # Adjust window height only
        new_height = self.centralWidget().sizeHint().height()
        if self.command_group.isVisible() and self.command_group.layout().sizeHint().height() > 0 :
             # if command group became visible and has content, ensure enough height
             min_content_height = self.command_group.layout().sizeHint().height() + self.feedback_group.minimumHeight() + self.toggle_command_button.height() + self.centralWidget().layout().spacing() * 2
             new_height = max(new_height, min_content_height)

        current_width = self.width()
        self.resize(current_width, new_height)

    def _update_config(self):
        self.config["run_command"] = self.command_entry.text()
        self.config["execute_automatically"] = self.auto_check.isChecked()
    
    def _update_gemini_config(self):
        """更新Gemini配置"""
        self.config["gemini_api_key"] = self.api_key_entry.text()
        self.config["gemini_model"] = self.model_combo.currentText()
        self.config["gemini_proxy"] = self.proxy_entry.text()
        
        # 立即保存配置到持久化存储
        self.settings.beginGroup(self.project_group_name)
        self.settings.setValue("gemini_api_key", self.config["gemini_api_key"])
        self.settings.setValue("gemini_model", self.config["gemini_model"])
        self.settings.setValue("gemini_proxy", self.config["gemini_proxy"])
        self.settings.endGroup()
        
        # 检查配置是否完整，决定是否启用图片识别按钮
        has_api_key = bool(self.config["gemini_api_key"].strip())
        has_image = self.current_image_data is not None
        self.analyze_image_button.setEnabled(has_api_key and has_image)

    def _append_log(self, text: str):
        self.log_buffer.append(text)
        self.log_text.append(text.rstrip())
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_text.setTextCursor(cursor)

    def _check_process_status(self):
        if self.process and self.process.poll() is not None:
            # Process has terminated
            exit_code = self.process.poll()
            self._append_log(f"\n进程已退出，退出代码: {exit_code}\n")
            self.run_button.setText("运行(&R)")
            self.process = None
            self.activateWindow()
            self.feedback_text.setFocus()

    def _run_command(self):
        if self.process:
            kill_tree(self.process)
            self.process = None
            self.run_button.setText("运行(&R)")
            return

        # Clear the log buffer but keep UI logs visible
        self.log_buffer = []

        command = self.command_entry.text()
        if not command:
            self._append_log("请输入要运行的命令\n")
            return

        self._append_log(f"$ {command}\n")
        self.run_button.setText("停止(&T)")

        try:
            self.process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.project_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=get_user_environment(),
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="ignore",
                close_fds=True,
            )

            def read_output(pipe):
                for line in iter(pipe.readline, ""):
                    self.log_signals.append_log.emit(line)

            threading.Thread(
                target=read_output,
                args=(self.process.stdout,),
                daemon=True
            ).start()

            threading.Thread(
                target=read_output,
                args=(self.process.stderr,),
                daemon=True
            ).start()

            # Start process status checking
            self.status_timer = QTimer()
            self.status_timer.timeout.connect(self._check_process_status)
            self.status_timer.start(100)  # Check every 100ms

        except Exception as e:
            self._append_log(f"运行命令时出错: {str(e)}\n")
            self.run_button.setText("运行(&R)")

    def _upload_image(self):
        """上传图片文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "选择图片文件", 
            "", 
            "图片文件 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;所有文件 (*)"
        )
        
        if file_path:
            try:
                with open(file_path, 'rb') as f:
                    image_data = f.read()
                self._set_image(image_data)
            except Exception as e:
                self._append_log(f"加载图片失败: {str(e)}\n")
    
    def _clear_image(self):
        """清除当前图片"""
        self.current_image_data = None
        self.image_preview_label.clear()
        self.image_preview_label.setText("支持拖拽图片到此处或使用Ctrl+V粘贴")
        self.image_preview_label.setStyleSheet("border: 2px dashed #aaa; padding: 20px;")
        self.clear_image_button.setEnabled(False)
        self._update_gemini_config()  # 更新按钮状态
    
    def _handle_image_paste(self, image_data: bytes):
        """处理粘贴的图片"""
        self._set_image(image_data)
    
    def _set_image(self, image_data: bytes):
        """设置图片数据并更新预览"""
        try:
            self.current_image_data = image_data
            
            # 创建图片预览
            pixmap = QPixmap()
            pixmap.loadFromData(image_data)
            
            # 缩放图片以适应预览区域
            if not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(
                    300, 150, 
                    Qt.KeepAspectRatio, 
                    Qt.SmoothTransformation
                )
                self.image_preview_label.setPixmap(scaled_pixmap)
                self.image_preview_label.setStyleSheet("border: 2px solid #4CAF50; padding: 5px;")
                self.clear_image_button.setEnabled(True)
                self._update_gemini_config()  # 更新按钮状态
            else:
                raise ValueError("无效的图片数据")
                
        except Exception as e:
            self._append_log(f"设置图片失败: {str(e)}\n")
    
    def _analyze_image(self):
        """使用Gemini分析图片"""
        if not self.current_image_data:
            self._append_log("请先上传或粘贴图片\n")
            return
            
        if not self.config["gemini_api_key"].strip():
            self._append_log("请先设置Gemini API Key\n")
            return
        
        # 获取分析提示内容
        text_content = self.analysis_prompt_entry.text().strip()
        if not text_content:
            text_content = "请详细分析这张图片的内容，包括图片中的对象、文字、场景等信息。"
        
        # 总是在提示中加上"请用中文回复"
        if "请用中文" not in text_content:
            text_content = f"{text_content} 请用中文回复。"
        
        # 禁用按钮并显示处理状态
        self.analyze_image_button.setEnabled(False)
        self.analyze_image_button.setText("识别中...")
        
        # 启动Gemini工作线程
        self.gemini_worker = GeminiWorker(
            api_key=self.config["gemini_api_key"],
            model=self.config["gemini_model"],
            proxy=self.config["gemini_proxy"],
            text=text_content,
            image_data=self.current_image_data
        )
        
        self.gemini_worker.finished.connect(self._handle_gemini_result)
        self.gemini_worker.error.connect(self._handle_gemini_error)
        self.gemini_worker.start()
    
    def _handle_gemini_result(self, result: str):
        """处理Gemini识别结果"""
        self.analyze_image_button.setText("图片识别")
        self._update_gemini_config()  # 恢复按钮状态
        
        # 将结果添加到反馈文本框
        current_text = self.feedback_text.toPlainText()
        if current_text and not current_text.endswith('\n'):
            current_text += '\n'
        
        new_text = current_text + "\n=== Gemini图片识别结果 ===\n" + result + "\n"
        self.feedback_text.setPlainText(new_text)
        
        # 移动光标到末尾
        cursor = self.feedback_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.feedback_text.setTextCursor(cursor)
        
        self._append_log("图片识别完成\n")
    
    def _handle_gemini_error(self, error: str):
        """处理Gemini识别错误"""
        self.analyze_image_button.setText("图片识别")
        self._update_gemini_config()  # 恢复按钮状态
        self._append_log(f"图片识别失败: {error}\n")

    def _submit_feedback(self):
        feedback_content = self.feedback_text.toPlainText().strip()
        # 在反馈内容末尾自动添加提醒文字
        if feedback_content:
            feedback_content += "\n执行完前面的内容后，请记得调用interactive_feedback。"
        else:
            feedback_content = "执行完前面的内容后，请记得调用interactive_feedback。"
        
        self.feedback_result = FeedbackResult(
            logs="".join(self.log_buffer),
            interactive_feedback=feedback_content,
        )
        self.close()

    def clear_logs(self):
        self.log_buffer = []
        self.log_text.clear()

    def _save_config(self):
        # Save run_command and execute_automatically to QSettings under project group
        self.settings.beginGroup(self.project_group_name)
        self.settings.setValue("run_command", self.config["run_command"])
        self.settings.setValue("execute_automatically", self.config["execute_automatically"])
        self.settings.setValue("gemini_api_key", self.config["gemini_api_key"])
        self.settings.setValue("gemini_model", self.config["gemini_model"])
        self.settings.setValue("gemini_proxy", self.config["gemini_proxy"])
        self.settings.endGroup()
        self._append_log("该项目的配置已保存。\n")

    def closeEvent(self, event):
        # Save general UI settings for the main window (geometry, state)
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        self.settings.endGroup()

        # Save project-specific command section visibility (this is now slightly redundant due to immediate save in toggle, but harmless)
        self.settings.beginGroup(self.project_group_name)
        self.settings.setValue("commandSectionVisible", self.command_group.isVisible())
        self.settings.endGroup()

        if self.process:
            kill_tree(self.process)
        super().closeEvent(event)

    def run(self) -> FeedbackResult:
        self.show()
        QApplication.instance().exec()

        if self.process:
            kill_tree(self.process)

        if not self.feedback_result:
            return FeedbackResult(logs="".join(self.log_buffer), interactive_feedback="")

        return self.feedback_result

def get_project_settings_group(project_dir: str) -> str:
    # Create a safe, unique group name from the project directory path
    # Using only the last component + hash of full path to keep it somewhat readable but unique
    basename = os.path.basename(os.path.normpath(project_dir))
    full_hash = hashlib.md5(project_dir.encode('utf-8')).hexdigest()[:8]
    return f"{basename}_{full_hash}"

def feedback_ui(project_directory: str, prompt: str, output_file: Optional[str] = None) -> Optional[FeedbackResult]:
    app = QApplication.instance() or QApplication()
    app.setPalette(get_dark_mode_palette(app))
    app.setStyle("Fusion")
    ui = FeedbackUI(project_directory, prompt)
    result = ui.run()

    if output_file and result:
        # Ensure the directory exists
        os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else ".", exist_ok=True)
        # Save the result to the output file
        with open(output_file, "w") as f:
            json.dump(result, f)
        return None

    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行反馈界面")
    parser.add_argument("--project-directory", default=os.getcwd(), help="运行命令的项目目录")
    parser.add_argument("--prompt", default="我已实现您请求的更改。", help="向用户显示的提示信息")
    parser.add_argument("--output-file", help="将反馈结果保存为JSON文件的路径")
    args = parser.parse_args()

    result = feedback_ui(args.project_directory, args.prompt, args.output_file)
    if result:
        print(f"\n收集的日志: \n{result['logs']}")
        print(f"\n收到的反馈:\n{result['interactive_feedback']}")
    sys.exit(0)
