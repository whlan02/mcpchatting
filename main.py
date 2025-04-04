import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Dict, List, Optional, Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from settings_dialog import SettingsDialog
from openai import OpenAI


from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_community.vectorstores import FAISS

import markdown

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
load_dotenv()
openai_api_key = os.getenv("OPENAI_API_KEY")

# Embeddings + Vectorstore
embedding = OpenAIEmbeddings(openai_api_key=openai_api_key)
vectorstore = FAISS.load_local("qgis_vector_index", embedding, allow_dangerous_deserialization=True)
retriever = vectorstore.as_retriever()

# LLMs
llm = ChatOpenAI(model_name="gpt-4o-mini", openai_api_key=openai_api_key)
general_llm = ChatOpenAI(model_name="gpt-4o-mini", openai_api_key=openai_api_key)

# Prompts for RAG
contextualize_q_system_prompt = """Given a chat history and the latest user question \
which might reference context in the chat history, formulate a standalone question \
which can be understood without the chat history. Do NOT answer the question, \
just reformulate it if needed and otherwise return it as is."""
contextualize_q_prompt = ChatPromptTemplate.from_messages([
    ("system", contextualize_q_system_prompt),
    MessagesPlaceholder("chat_history"),
    ("human", "{input}"),
])
history_aware_retriever = create_history_aware_retriever(llm, retriever, contextualize_q_prompt)

qa_system_prompt = """You are an assistant specialized in GIS and remote sensing, with access to QGIS via the Model Context Protocol (MCP).

You can:
- Answer geospatial questions concisely,
- Generate QGIS expressions and processing steps,
- AND directly trigger QGIS operations by returning structured MCP function calls.

Guidelines:
- If a user asks for a spatial operation (e.g., NDVI, buffer, clip, reproject), generate and return a valid MCP command for it.
- If context is available, use it. If not, fall back on your own knowledge.
- Always return a plain-language explanation *plus* the MCP function in a code block (JSON format).
- Only return "I don't know" if you cannot answer or trigger anything.

Respond in 3 sentences or less unless detailed steps are needed.

{context}"""

qa_prompt = ChatPromptTemplate.from_messages([
    MessagesPlaceholder("chat_history"),
    ("human", "Use the following context to answer the question.\n\n"
              "**Instructions:**\n"
              "- Use **bold** for key terms.\n"
              "- Use bullet points for lists.\n"
              "- Format inline code with backticks.\n"
              "- Use triple backticks and specify `json` for JSON blocks.\n\n"
              "Context:\n{context}\n\n"
              "Now, answer the user's question."),
])
question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)
rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)

# Chat History Store
store = {}
def get_session_history(session_id: str) -> BaseChatMessageHistory:
    if session_id not in store:
        store[session_id] = ChatMessageHistory()
    return store[session_id]

# Full Chain with memory
conversational_rag_chain = RunnableWithMessageHistory(
    rag_chain,
    get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history",
    output_messages_key="answer"
)

# Import GUI components conditionally
try:
    from PySide6.QtWidgets import (QApplication, QMainWindow, QVBoxLayout, QHBoxLayout, 
                                QTextEdit, QLineEdit, QPushButton, QWidget, QLabel,
                                QScrollArea, QSizePolicy)
    from PySide6.QtCore import QObject, Signal, Slot, Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False

class Configuration:
    """Manages configuration and environment variables for the MCP client."""

    def __init__(self) -> None:
        """Initialize configuration."""
        # Remove environment variable loading
        self.api_key = None  # Will be set through settings dialog

    @staticmethod
    def load_env() -> None:
        """Load environment variables from .env file."""
        load_dotenv()

    @staticmethod
    def load_config(file_path: str) -> Dict[str, Any]:
        """Load server configuration from JSON file.
        
        Args:
            file_path: Path to the JSON configuration file.
            
        Returns:
            Dict containing server configuration.
            
        Raises:
            FileNotFoundError: If configuration file doesn't exist.
            JSONDecodeError: If configuration file is invalid JSON.
        """
        with open(file_path, 'r') as f:
            return json.load(f)

    @property
    def llm_api_key(self) -> str:
        """Get the LLM API key.
        
        Returns:
            The API key as a string.
            
        Raises:
            ValueError: If the API key is not set in settings.
        """
        if not self.api_key:
            raise ValueError("Please set API key in settings")
        return self.api_key


class Server:
    """Manages MCP server connections and tool execution."""

    def __init__(self, name: str, config: Dict[str, Any]) -> None:
        self.name: str = name
        self.config: Dict[str, Any] = config
        self.stdio_context: Optional[Any] = None
        self.session: Optional[ClientSession] = None
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()
        self.capabilities: Optional[Dict[str, Any]] = None

    async def initialize(self) -> None:
        """Initialize the server connection."""
        server_params = StdioServerParameters(
            command=shutil.which("npx") if self.config['command'] == "npx" else self.config['command'],
            args=self.config['args'],
            env={**os.environ, **self.config['env']} if self.config.get('env') else None
        )
        try:
            self.stdio_context = stdio_client(server_params)
            read, write = await self.stdio_context.__aenter__()
            self.session = ClientSession(read, write)
            await self.session.__aenter__()
            self.capabilities = await self.session.initialize()
        except Exception as e:
            logging.error(f"Error initializing server {self.name}: {e}")
            await self.cleanup()
            raise

    async def list_tools(self) -> List[Any]:
        """List available tools from the server.
        
        Returns:
            A list of available tools.
            
        Raises:
            RuntimeError: If the server is not initialized.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")
        
        tools_response = await self.session.list_tools()
        tools = []
        
        supports_progress = (
            self.capabilities 
            and 'progress' in self.capabilities
        )
        
        if supports_progress:
            logging.info(f"Server {self.name} supports progress tracking")
        
        for item in tools_response:
            if isinstance(item, tuple) and item[0] == 'tools':
                for tool in item[1]:
                    tools.append(Tool(tool.name, tool.description, tool.inputSchema))
                    if supports_progress:
                        logging.info(f"Tool '{tool.name}' will support progress tracking")
        
        return tools

    async def execute_tool(
        self, 
        tool_name: str, 
        arguments: Dict[str, Any], 
        retries: int = 2, 
        delay: float = 1.0
    ) -> Any:
        """Execute a tool with retry mechanism.
        
        Args:
            tool_name: Name of the tool to execute.
            arguments: Tool arguments.
            retries: Number of retry attempts.
            delay: Delay between retries in seconds.
            
        Returns:
            Tool execution result.
            
        Raises:
            RuntimeError: If server is not initialized.
            Exception: If tool execution fails after all retries.
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")

        attempt = 0
        while attempt < retries:
            try:
                supports_progress = (
                    self.capabilities 
                    and 'progress' in self.capabilities
                )

                if supports_progress:
                    logging.info(f"Executing {tool_name} with progress tracking...")
                    result = await self.session.call_tool(
                        tool_name, 
                        arguments,
                        progress_token=f"{tool_name}_execution"
                    )
                else:
                    logging.info(f"Executing {tool_name}...")
                    result = await self.session.call_tool(tool_name, arguments)

                return result

            except Exception as e:
                attempt += 1
                logging.warning(f"Error executing tool: {e}. Attempt {attempt} of {retries}.")
                if attempt < retries:
                    logging.info(f"Retrying in {delay} seconds...")
                    await asyncio.sleep(delay)
                else:
                    logging.error("Max retries reached. Failing.")
                    raise

    async def cleanup(self) -> None:
        """Clean up server resources."""
        async with self._cleanup_lock:
            try:
                if self.session:
                    try:
                        await self.session.__aexit__(None, None, None)
                    except Exception as e:
                        logging.warning(f"Warning during session cleanup for {self.name}: {e}")
                    finally:
                        self.session = None

                if self.stdio_context:
                    try:
                        await self.stdio_context.__aexit__(None, None, None)
                    except (RuntimeError, asyncio.CancelledError) as e:
                        logging.info(f"Note: Normal shutdown message for {self.name}: {e}")
                    except Exception as e:
                        logging.warning(f"Warning during stdio cleanup for {self.name}: {e}")
                    finally:
                        self.stdio_context = None
            except Exception as e:
                logging.error(f"Error during cleanup of server {self.name}: {e}")


class Tool:
    """Represents a tool with its properties and formatting."""

    def __init__(self, name: str, description: str, input_schema: Dict[str, Any]) -> None:
        self.name: str = name
        self.description: str = description
        self.input_schema: Dict[str, Any] = input_schema

    def format_for_llm(self) -> str:
        """Format tool information for LLM.
        
        Returns:
            A formatted string describing the tool.
        """
        args_desc = []
        if 'properties' in self.input_schema:
            for param_name, param_info in self.input_schema['properties'].items():
                arg_desc = f"- {param_name}: {param_info.get('description', 'No description')}"
                if param_name in self.input_schema.get('required', []):
                    arg_desc += " (required)"
                args_desc.append(arg_desc)
        
        return f"""
Tool: {self.name}
Description: {self.description}
Arguments:
{chr(10).join(args_desc)}
"""


class LLMClient:
    """Manages communication with various LLM providers."""
    def __init__(self) -> None:
        # 初始化时不再要求API key
        self.api_key: str = None
        self.provider: str = "OpenAI"
        self.model: str = "gpt-4o-mini"
        self._openai_client = None
        self._anthropic_client = None
        self._google_client = None

    def update_settings(self, provider: str, model: str, api_key: str) -> None:
        """Update client settings."""
        self.provider = provider
        self.model = model
        self.api_key = api_key
        # Reset all clients when settings change
        self._openai_client = None
        self._anthropic_client = None
        self._google_client = None

    def get_response(self, messages: List[Dict[str, str]]) -> str:
        """Get a response from the selected LLM provider."""
        if not self.api_key:
            return "Please set your API key in settings first."
        
        try:
            if self.provider == "OpenAI":
                return self._get_openai_response(messages)
            elif self.provider == "Anthropic":
                return self._get_anthropic_response(messages)
            elif self.provider == "Google":
                return self._get_google_response(messages)
            elif self.provider == "Deepseek":
                return self._get_deepseek_response(messages)
            else:
                error_msg = f"Unsupported provider: {self.provider}"
                logging.error(error_msg)
                return error_msg
        except Exception as e:
            error_msg = f"Error getting response from {self.provider}: {str(e)}"
            logging.error(error_msg)
            return error_msg

    def _get_openai_response(self, messages: List[Dict[str, str]]) -> str:
        """Get response using OpenAI's official client."""
        try:
            client = self._get_openai_client()
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=4096,
                top_p=1
            )
            raw_text = response.choices[0].message.content
            rendered_html = markdown.markdown(raw_text)  # Convert Markdown to HTML
            return rendered_html
        except Exception as e:
            error_msg = f"OpenAI API error: {str(e)}"
            logging.error(error_msg)
            return error_msg

    def _get_anthropic_response(self, messages: List[Dict[str, str]]) -> str:
        """Get response using Anthropic's official client."""
        try:
            client = self._get_anthropic_client()
            # Filter out system messages as they're handled differently in Anthropic
            chat_messages = [msg for msg in messages if msg["role"] != "system"]
            
            response = client.messages.create(
                model=self.model,
                max_tokens=4096,
                messages=chat_messages
            )
            return response.content
        except Exception as e:
            error_msg = f"Anthropic API error: {str(e)}"
            logging.error(error_msg)
            return error_msg

    def _get_google_response(self, messages: List[Dict[str, str]]) -> str:
        """Get response using Google's official client."""
        try:
            client = self._get_google_client()
            
            # Combine all messages into a single conversation string
            conversation = "\n".join([
                f"{msg['role']}: {msg['content']}" 
                for msg in messages 
                if msg['role'] != 'system'
            ])
            
            response = client.models.generate_content(
                model=self.model,
                contents=conversation
            )
            return response.text
        except Exception as e:
            error_msg = f"Google API error: {str(e)}"
            logging.error(error_msg)
            return error_msg

    def _get_deepseek_response(self, messages: List[Dict[str, str]]) -> str:
        """Get response from Deepseek using OpenAI SDK."""
        try:
            client = self._get_openai_client(base_url="https://api.deepseek.com")
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False
            )
            return response.choices[0].message.content
        except Exception as e:
            error_msg = f"Deepseek API error: {str(e)}"
            logging.error(error_msg)
            return error_msg

    def _get_openai_client(self, base_url=None):
        """Get or create OpenAI client with current settings."""
        if self._openai_client is None:
            kwargs = {"api_key": self.api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self._openai_client = OpenAI(**kwargs)
        return self._openai_client

    def _get_anthropic_client(self):
        """Get or create Anthropic client with current settings."""
        if self._anthropic_client is None:
            import anthropic
            self._anthropic_client = anthropic.Anthropic(api_key=self.api_key)
        return self._anthropic_client

    def _get_google_client(self):
        """Get or create Google client with current settings."""
        if self._google_client is None:
            from google import genai
            self._google_client = genai.Client(api_key=self.api_key)
        return self._google_client


# GUI Components
if GUI_AVAILABLE:
    class SignalEmitter(QObject):
        update_chat = Signal(str, str)  # role, content
        tool_progress = Signal(int, int)  # progress, total

    class ChatBubbleWidget(QWidget):
        def __init__(self, text, role="user", parent=None):
            super().__init__(parent)
            layout = QHBoxLayout(self)
            layout.setContentsMargins(10, 5, 10, 5)
            
            # Create bubble text
            self.bubble = QLabel()
            self.bubble.setWordWrap(True)
            self.bubble.setMinimumWidth(500)
            self.bubble.setMaximumWidth(700)
            self.bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            
            # Format text based on role
            formatted_text = f'<div style="color: #2C3E50; font-size: 14px; line-height: 1.5;">{text}</div>'
            self.bubble.setText(formatted_text)
            self.bubble.setTextFormat(Qt.RichText)
            
            is_user = role == "user"
            self.bubble.setStyleSheet(f"""
                QLabel {{
                    background-color: {'#DCF8C6' if is_user else '#FFFFFF'};
                    border-radius: 10px;
                    padding: 12px;
                    margin: {'0 10px 0 50px' if is_user else '0 50px 0 10px'};
                }}
            """)
            
            # Add avatar
            avatar = QLabel("👤" if is_user else "🤖")
            avatar.setStyleSheet("""
                QLabel {
                    font-size: 24px;
                    margin: 5px;
                }
            """)
            
            # Arrange layout based on role
            if is_user:
                layout.addStretch()
                layout.addWidget(self.bubble)
                layout.addWidget(avatar)
            else:
                layout.addWidget(avatar)
                layout.addWidget(self.bubble)
                layout.addStretch()

    class ChatArea(QScrollArea):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWidgetResizable(True)
            self.setStyleSheet("""
                QScrollArea {
                    border: none;
                    background-color: #F8F9FA;
                }
            """)
            
            self.container = QWidget()
            self.container_layout = QVBoxLayout(self.container)
            self.container_layout.addStretch()
            self.setWidget(self.container)
        
        def add_message(self, text, role="user"):
            self.container_layout.takeAt(self.container_layout.count() - 1)
            bubble = ChatBubbleWidget(text, role)
            self.container_layout.addWidget(bubble)
            self.container_layout.addStretch()
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

class ChatSession:
    """Base chat session class for both terminal and GUI modes."""
    def __init__(self, servers: List[Server], llm_client: LLMClient) -> None:
        self.servers = servers
        self.llm_client = llm_client
        self.messages = []
        self.all_tools = []
        self.session_id = "default-session"  # Add session ID for RAG

    async def cleanup_servers(self) -> None:
        """Clean up all servers properly."""
        cleanup_tasks = []
        for server in self.servers:
            cleanup_tasks.append(asyncio.create_task(server.cleanup()))
        
        if cleanup_tasks:
            try:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            except Exception as e:
                logging.warning(f"Warning during final cleanup: {e}")

    async def process_llm_response(self, llm_response: str) -> str:
        """Process the LLM response and execute tools if needed."""
        if not llm_response:
            return "Sorry, I received no response from the language model. Please try again."
        
        try:
            tool_call = json.loads(llm_response)
            if isinstance(tool_call, dict) and "tool" in tool_call and "arguments" in tool_call:
                return await self._execute_tool(tool_call)
            return llm_response
        except json.JSONDecodeError:
            return llm_response  # Not a JSON response, return as is

    async def _execute_tool(self, tool_call: dict) -> str:
        """Execute a single tool call."""
        logging.info(f"Executing tool: {tool_call['tool']}")
        logging.info(f"With arguments: {tool_call['arguments']}")
        
        for server in self.servers:
            tools = await server.list_tools()
            if any(tool.name == tool_call["tool"] for tool in tools):
                try:
                    result = await server.execute_tool(tool_call["tool"], tool_call["arguments"])
                    return f"Tool execution result: {result}"
                except Exception as e:
                    error_msg = f"Error executing tool: {str(e)}"
                    logging.error(error_msg)
                    return error_msg
        
        return f"No server found with tool: {tool_call['tool']}"

    async def initialize_servers(self) -> bool:
        """Initialize all servers and collect tools."""
        for server in self.servers:
            try:
                await server.initialize()
            except Exception as e:
                logging.error(f"Failed to initialize server: {e}")
                await self.cleanup_servers()
                return False
        
        for server in self.servers:
            tools = await server.list_tools()
            self.all_tools.extend(tools)
        
        return True

    def setup_system_message(self) -> None:
        """Set up the system message with tools information."""
        tools_description = "\n".join([tool.format_for_llm() for tool in self.all_tools])
        
        system_message = f"""You are a helpful assistant with access to these tools: 

{tools_description}
Choose the appropriate tool based on the user's question. If no tool is needed, reply directly.

IMPORTANT: When you need to use a tool, you must ONLY respond with the exact JSON object format below, nothing else:
{{
    "tool": "tool-name",
    "arguments": {{
        "argument-name": "value"
    }}
}}

After receiving a tool's response:
1. Transform the raw data into a natural, conversational response
2. Keep responses concise but informative
3. Focus on the most relevant information
4. Use appropriate context from the user's question
5. Avoid simply repeating the raw data

Please use only the tools that are explicitly defined above."""

        self.messages = [
            {
                "role": "system",
                "content": system_message
            }
        ]

    async def start_terminal(self) -> None:
        """Start the chat session in terminal mode."""
        logging.info("Starting chat in terminal mode")
        try:
            if not await self.initialize_servers():
                return
            
            self.setup_system_message()
            
            print("\nWelcome to the AI Assistant. Type 'exit' or 'quit' to end the session.")
            
            while True:
                try:
                    user_input = input("\nYou: ").strip()
                    if user_input.lower() in ['quit', 'exit']:
                        logging.info("\nExiting...")
                        break

                    self.messages.append({"role": "user", "content": user_input})
                    
                    llm_response = self.llm_client.get_response(self.messages)
                    print(f"\nAssistant: {llm_response}")

                    result = await self.process_llm_response(llm_response)
                    
                    if result != llm_response:
                        self.messages.append({"role": "assistant", "content": llm_response})
                        self.messages.append({"role": "system", "content": result})
                        
                        final_response = self.llm_client.get_response(self.messages)
                        print(f"\nFinal response: {final_response}")
                        self.messages.append({"role": "assistant", "content": final_response})
                    else:
                        self.messages.append({"role": "assistant", "content": llm_response})

                except KeyboardInterrupt:
                    logging.info("\nExiting...")
                    break
        
        finally:
            await self.cleanup_servers()

if GUI_AVAILABLE:
    class ChatSessionGUI(ChatSession):
        """GUI version of ChatSession."""
        def __init__(self, servers: List[Server], llm_client: LLMClient) -> None:
            super().__init__(servers, llm_client)
            self.signals = SignalEmitter()

        async def process_llm_response(self, llm_response: str) -> str:
            """Process the LLM response and execute tools if needed."""
            result = await super().process_llm_response(llm_response)
            if result != llm_response:
                self.signals.update_chat.emit("system", f"Tool execution: {result}")
            return result

        async def send_message(self, user_input: str) -> None:
            """Process a user message and get response."""
            self.messages.append({"role": "user", "content": user_input})
            self.signals.update_chat.emit("user", user_input)
            
            llm_response = self.llm_client.get_response(self.messages)
            self.signals.update_chat.emit("assistant", llm_response)
            
            result = await self.process_llm_response(llm_response)
            
            if result != llm_response:
                self.messages.append({"role": "assistant", "content": llm_response})
                self.messages.append({"role": "system", "content": result})
                
                final_response = self.llm_client.get_response(self.messages)
                self.signals.update_chat.emit("assistant", final_response)
                self.messages.append({"role": "assistant", "content": final_response})
            else:
                self.messages.append({"role": "assistant", "content": llm_response})

    class MainWindow(QMainWindow):
        def __init__(self, chat_session):
            super().__init__()
            self.chat_session = chat_session
            self.init_ui()
            
        def init_ui(self):
            self.setWindowTitle("AI Chat Interface")
            screen = QGuiApplication.primaryScreen().availableGeometry()
            self.resize(int(screen.width() * 0.6), int(screen.height() * 0.6))
            
            main_widget = QWidget()
            main_layout = QVBoxLayout(main_widget)
            main_layout.setSpacing(10)
            main_layout.setContentsMargins(20, 20, 20, 20)
            
            self.chat_area = ChatArea()
            main_layout.addWidget(self.chat_area, stretch=1)
            
            progress_layout = QHBoxLayout()
            self.progress_label = QLabel("Tool execution progress:")
            self.progress_label.setVisible(False)
            progress_layout.addWidget(self.progress_label)
            main_layout.addLayout(progress_layout)
            
            input_container = QWidget()
            input_container.setFixedHeight(100)
            input_container.setStyleSheet("""
                QWidget {
                    background-color: white;
                    border: 1px solid #E8E8E8;
                    border-radius: 10px;
                }
            """)
            
            input_layout = QHBoxLayout(input_container)
            input_layout.setContentsMargins(10, 5, 10, 5)
            
            # Add settings button
            self.settings_button = QPushButton("⚙️")
            self.settings_button.setFixedWidth(40)
            self.settings_button.setStyleSheet("""
                QPushButton {
                    background-color: transparent;
                    border: none;
                    font-size: 20px;
                }
                QPushButton:hover {
                    background-color: #E8E8E8;
                    border-radius: 5px;
                }
            """)
            self.settings_button.clicked.connect(self.show_settings)
            
            self.input_field = QTextEdit()
            self.input_field.setPlaceholderText("Type your message here...")
            self.input_field.setStyleSheet("""
                QTextEdit {
                    border: none;
                    padding: 5px;
                    font-size: 14px;
                    background-color: white;
                }
            """)
            self.input_field.installEventFilter(self)
            
            self.send_button = QPushButton("Send")
            self.send_button.setFixedWidth(80)
            self.send_button.setStyleSheet("""
                QPushButton {
                    background-color: #87CEEB;
                    color: white;
                    border: none;
                    border-radius: 5px;
                    padding: 8px 20px;
                    font-size: 14px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #5CACEE;
                }
            """)
            self.send_button.clicked.connect(self.send_message)
            
            input_layout.addWidget(self.settings_button)
            input_layout.addWidget(self.input_field)
            input_layout.addWidget(self.send_button)
            main_layout.addWidget(input_container)
            
            self.setCentralWidget(main_widget)
            
            self.chat_session.signals.update_chat.connect(self.update_chat_display)
            self.chat_session.signals.tool_progress.connect(self.update_progress)
            
            self.update_chat_display("system", "Welcome to the AI Assistant. How can I help you today?")
        
        def eventFilter(self, obj, event):
            if obj is self.input_field and event.type() == 6:  # KeyPress event
                if event.key() == Qt.Key_Return and not event.modifiers():
                    self.send_message()
                    return True
                elif event.key() == Qt.Key_Return and event.modifiers() == Qt.ShiftModifier:
                    cursor = self.input_field.textCursor()
                    cursor.insertText('\n')
                    return True
            return super().eventFilter(obj, event)
        
        @Slot(str, str)
        def update_chat_display(self, role, content):
            """Update the chat display with new messages."""
            self.chat_area.add_message(content, role)
        
        @Slot(int, int)
        def update_progress(self, progress, total):
            """Update the progress bar."""
            percentage = int((progress / total) * 100)
            self.progress_label.setText(f"Tool execution progress: {progress}/{total} ({percentage}%)")
            self.progress_label.setVisible(True)
            QTimer.singleShot(5000, lambda: self.progress_label.setVisible(False))
        
        def send_message(self):
            """Send the message from the input field."""
            user_input = self.input_field.toPlainText().strip()
            if not user_input:
                return
            
            self.input_field.clear()
            self.input_field.setEnabled(False)
            self.send_button.setEnabled(False)
            
            asyncio.ensure_future(self.process_message(user_input))
        
        async def process_message(self, user_input):
            """Process the message asynchronously."""
            await self.chat_session.send_message(user_input)
            self.input_field.setEnabled(True)
            self.send_button.setEnabled(True)
            self.input_field.setFocus()

        def show_settings(self):
            dialog = SettingsDialog(self)
            if dialog.exec():
                settings = dialog.get_settings()
                # Update the LLM client with new settings
                self.chat_session.llm_client.update_settings(
                    provider=settings["provider"],
                    model=settings["model"],
                    api_key=settings["api_key"]
                )

async def run_gui_mode():
    """Run the application in GUI mode."""
    app = QApplication(sys.argv)
    
    config = Configuration()
    server_config = config.load_config('servers_config.json')
    servers = [Server(name, srv_config) for name, srv_config in server_config['mcpServers'].items()]
    # 创建LLMClient时不传入api_key
    llm_client = LLMClient()
    
    chat_session = ChatSessionGUI(servers, llm_client)
    
    if not await chat_session.initialize_servers():
        logging.error("Failed to initialize application. Exiting.")
        return
    
    chat_session.setup_system_message()
    
    window = MainWindow(chat_session)
    window.show()
    
    app.aboutToQuit.connect(lambda: asyncio.ensure_future(chat_session.cleanup_servers()))
    
    app.chat_session = chat_session
    
    while True:
        app.processEvents()
        await asyncio.sleep(0.01)
        if not window.isVisible():
            break
    
    await chat_session.cleanup_servers()

async def main() -> None:
    """Initialize and run the chat session in GUI mode by default."""
    import argparse
    parser = argparse.ArgumentParser(description='AI Chat Application')
    parser.add_argument('--terminal', action='store_true', help='Run in terminal mode')
    args = parser.parse_args()
    
    if args.terminal:
        logging.info("Starting terminal mode")
        config = Configuration()
        server_config = config.load_config('servers_config.json')
        servers = [Server(name, srv_config) for name, srv_config in server_config['mcpServers'].items()]
        llm_client = LLMClient()
        chat_session = ChatSession(servers, llm_client)
        await chat_session.start_terminal()
    elif GUI_AVAILABLE:
        logging.info("Starting GUI mode")
        await run_gui_mode()
    else:
        logging.error("PySide6 is not installed. Please install it with 'pip install pyside6' to use GUI mode.")
        logging.info("Falling back to terminal mode")
        
        config = Configuration()
        server_config = config.load_config('servers_config.json')
        servers = [Server(name, srv_config) for name, srv_config in server_config['mcpServers'].items()]
        llm_client = LLMClient()
        chat_session = ChatSession(servers, llm_client)
        await chat_session.start_terminal()

if __name__ == "__main__":
    asyncio.run(main())