"""
core/agents/builtin.py — the specialist agents (eleven from Phase 4, plus the
Phase 5 build team: Manager, UI, Testing and Review; Research and Coding
double as team members).

Each is small on purpose: the heavy lifting lives in the existing actions/
modules (wrapped as tools in core/tools/builtin.py). An agent contributes
(a) routing vocabulary, (b) tool ownership, (c) optional argument
preparation for its domain.
"""
from __future__ import annotations

from core.agents.base import Agent, AgentRegistry


class GeneralAgent(Agent):
    name = "general"
    title = "General Agent"
    description = "Conversation, reasoning, summarising text, memory and anything no specialist owns."
    keywords = {"remember": 3, "forget": 3, "what do you know": 3, "explain": 2, "tell me": 1,
                "summary": 1, "undo": 3, "mode": 1}
    tool_prefixes = ("ai.", "memory.", "text.")
    extra_tools = ("save_memory", "recall_memory", "forget_memory", "set_mode", "undo")


class LaptopAgent(Agent):
    name = "laptop"
    title = "Laptop Agent"
    description = "Launching applications, windows, keyboard/mouse control, desktop, games."
    keywords = {"launch": 3, "application": 2, " app": 2, "app ": 2, "window": 2, "minimize": 3,
                "maximize": 3, "wallpaper": 3, "desktop": 1, "type ": 1, "click": 2, "keyboard": 2,
                "mouse": 2, "vs code": 3, "vscode": 3, "notepad": 3, "spotify": 3, "steam": 3,
                "game": 2, "calculator": 3, "open": 1}
    tool_prefixes = ("app.", "laptop.")
    extra_tools = ("open_app", "desktop_control", "computer_control", "game_updater")


class BrowserAgent(Agent):
    name = "browser"
    title = "Browser Agent"
    description = "Driving the web browser: opening sites, tabs, forms, downloading files, YouTube."
    keywords = {"chrome": 4, "firefox": 4, "edge": 3, "browser": 4, "website": 3, "url": 3, "tab": 2,
                "download": 3, "youtube": 4, "open site": 3, "http": 3, "www": 3, "search for": 1,
                "google it": 2}
    tool_prefixes = ("browser.",)
    extra_tools = ("browser_control", "youtube_video")


class FileAgent(Agent):
    name = "file"
    title = "File Agent"
    description = "Finding, renaming, moving, copying, deleting, reading and analysing files and documents."
    keywords = {"file": 3, "files": 3, "folder": 3, "pdf": 3, "document": 2, "docx": 3, "rename": 4,
                "move": 3, "copy": 2, "delete": 3, "put it in": 3, "put them in": 3, "downloads": 1,
                "documents": 2, "desktop": 1, "find my": 3, "find the": 2, "spreadsheet": 2,
                "csv": 3, "zip": 2, "organize": 2, "read the": 2, "summarize it": 2, "summarise it": 2}
    tool_prefixes = ("file.", "document.")
    extra_tools = ("file_controller", "file_processor")


class CodingAgent(Agent):
    name = "coding"
    title = "Coding Agent"
    description = "Writing, running, explaining and fixing code; building small projects."
    keywords = {"code": 3, "script": 3, "python": 3, "program": 2, "debug": 3, "bug": 2, "function": 2,
                "compile": 3, "build a": 2, "project": 1, "javascript": 3, "error on screen": 3,
                "fix the bug": 4, "add a feature": 3, "refactor": 3, "fix my": 2}
    tool_prefixes = ("code.", "project.")
    extra_tools = ("code_helper", "dev_agent", "team.implement")


class ResearchAgent(Agent):
    name = "research"
    title = "Research Agent"
    description = "Web search, news, research summaries, prices, weather, flights, topic monitoring."
    keywords = {"search the web": 4, "web search": 4, "research": 4, "papers": 4, "paper": 3,
                "article": 2, "news": 4, "look up": 3, "search": 1, "latest": 2, "weather": 4,
                "flight": 4, "price": 3, "compare": 2, "who is": 2, "what is": 1, "summarize": 1,
                "summarise": 1, "monitor": 2}
    tool_prefixes = ("web.", "research.")
    extra_tools = ("web_search", "weather_report", "flight_finder", "manage_monitor", "team.research")


class CommunicationAgent(Agent):
    name = "communication"
    title = "Communication Agent"
    description = "Preparing and (with permission) sending messages — WhatsApp, Telegram, etc."
    keywords = {"message": 4, "send": 3, "whatsapp": 5, "telegram": 5, "text him": 4, "text her": 4,
                "tell arun": 2, "email": 4, "mail": 3, "reply": 2, "sms": 4, "discord": 4,
                "instagram": 3, "signal": 2}
    tool_prefixes = ("message.",)
    extra_tools = ("send_message",)


class SystemAgent(Agent):
    name = "system"
    title = "System Agent"
    description = "Volume, brightness, Wi-Fi, power, system health, JARVIS itself."
    keywords = {"volume": 4, "brightness": 4, "wifi": 4, "wi-fi": 4, "bluetooth": 3, "battery": 4,
                "cpu": 4, "ram": 3, "memory usage": 3, "disk": 2, "shutdown": 4, "restart": 4,
                "sleep": 2, "mute": 3, "system status": 4, "settings": 2, "lock": 2}
    tool_prefixes = ("system.",)
    extra_tools = ("computer_settings", "system_status", "shutdown_jarvis")


class VisionAgent(Agent):
    name = "vision"
    title = "Screen/Vision Agent"
    description = "Looking at the screen or camera and describing / reading what is there."
    keywords = {"screen": 3, "on my screen": 4, "camera": 4, "webcam": 4, "look at": 3, "see": 1,
                "what's this": 2, "read this": 2, "screenshot": 2}
    tool_prefixes = ("screen.", "vision.")
    extra_tools = ("screen_process", "close_camera")


class ReminderAgent(Agent):
    name = "reminder"
    title = "Reminder/Calendar Agent"
    description = "Reminders, alarms, and (future) calendar events."
    keywords = {"remind": 5, "reminder": 5, "alarm": 4, "calendar": 4, "schedule": 3, "meeting": 3,
                "tomorrow at": 2, "at pm": 1, "o'clock": 2}
    tool_prefixes = ("reminder.", "calendar.")
    extra_tools = ("reminder",)


class PluginAgent(Agent):
    name = "plugin"
    title = "Plugin Agent"
    description = "Tools contributed by drop-in plugins (plugins/*.py)."
    keywords = {"plugin": 3}
    tool_prefixes = ("plugin.",)


class ManagerAgent(Agent):
    name = "manager"
    title = "Manager Agent"
    description = "Leads multi-agent builds: turns a goal into a spec and hands work to the team."
    keywords = {"build me": 4, "make me a website": 5, "create a website": 5, "build a website": 5,
                "landing page": 4, "portfolio": 3, "web app": 4, "team": 2}
    extra_tools = ("team.plan",)


class UIAgent(Agent):
    name = "ui"
    title = "UI/Design Agent"
    description = ("Visual design and drawing: palette, typography, layout, components — and making pictures "
                  "(raster images when the model allows, otherwise vector SVG art that's rendered and checked).")
    keywords = {"design": 3, "layout": 3, "colour": 2, "color": 2, "ui": 3, "theme": 2, "draw": 4,
                "logo": 4, "icon": 3, "illustration": 4, "picture of": 3, "image of": 3, "svg": 4,
                "poster": 3, "graphic": 3}
    tool_prefixes = ("design.",)
    extra_tools = ("team.design",)


class TestingAgent(Agent):
    name = "testing"
    title = "Testing Agent"
    description = "Runs real checks on generated code: structure, syntax, broken links."
    keywords = {"test": 3, "check the code": 4, "validate": 3}
    extra_tools = ("team.test",)


class ReviewAgent(Agent):
    name = "review"
    title = "Review Agent"
    description = "Reviews results against the spec, drives fix rounds, gives the final verdict."
    keywords = {"review": 3, "code review": 5}
    extra_tools = ("team.review",)


class WebDevAgent(Agent):
    name = "webdev"
    title = "Web Dev Agent"
    description = ("Runs a website JARVIS built — serves it locally, drives a real browser over it to catch "
                  "errors and check how it looks, opens it, and manages the preview server.")
    keywords = {"run the site": 4, "run the website": 4, "open the site": 3, "preview": 3, "localhost": 2,
                "test the website": 4, "does the site work": 3, "serve": 2}
    tool_prefixes = ("process.",)
    extra_tools = ("web.run", "web.open")


def default_agents() -> AgentRegistry:
    return AgentRegistry([GeneralAgent(), LaptopAgent(), BrowserAgent(), FileAgent(), CodingAgent(),
                          ResearchAgent(), CommunicationAgent(), SystemAgent(), VisionAgent(),
                          ReminderAgent(), PluginAgent(), ManagerAgent(), UIAgent(), TestingAgent(),
                          ReviewAgent(), WebDevAgent()])
