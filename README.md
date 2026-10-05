# ⚙️ MARK LII

### A cross-platform, voice-first personal AI assistant

MARK LII is a modular personal AI assistant built around **real-time voice interaction, persistent memory, computer control, visual awareness, autonomous task execution, and an extensible plugin architecture**.

It combines a PyQt6 desktop HUD with Gemini Live voice interaction and a local tool/agent layer that can search the web, control applications, process files, monitor hardware, manage reminders, operate browsers, interact with YouTube, and more.

> **Project status:** Active personal AI assistant / experimental platform  
> **Primary voice backend:** Google Gemini Live  
> **Platforms:** Windows, macOS, Linux

---

## ✨ Highlights

| Capability | What MARK LII can do |
|---|---|
| 🎙️ **Real-time Voice** | Natural, low-latency voice conversations through Gemini Live |
| 🧠 **Persistent Memory** | Stores useful personal context and retrieves older facts locally when needed |
| 👁️ **Vision** | Uses screen capture and camera input for visual awareness |
| 🖥️ **Computer Control** | Launch apps, control windows, keyboard/mouse actions, volume, brightness, Wi-Fi and power operations |
| 🌐 **Web Research** | Search, news, research, price lookup and comparison workflows |
| 📂 **File Intelligence** | Read, summarize and work with local files |
| 🤖 **Agent Architecture** | Planning, orchestration, verification and multi-step task execution |
| 🧩 **Plugins** | Add capabilities by dropping Python plugins into `plugins/` |
| 💓 **Reactive HUD** | Voice-reactive waveform and reactor-style interface |
| 🎨 **Live Theming** | Change the HUD colour dynamically |
| 🎧 **Audio Devices** | Select and validate microphones and speakers |
| ↩️ **Undo** | Reverse supported file and system actions |
| ⚠️ **Confirmation Gates** | Human confirmation for selected irreversible operations |
| 🔔 **Proactive Assistant** | Time/context-aware reminders and background monitoring |
| 📊 **Hardware Monitoring** | CPU, RAM, GPU and temperature telemetry |
| ✈️ **Flight Finder** | Live flight price and availability lookup |
| 🌤️ **Weather** | Live weather reporting |
| ⏰ **Reminders** | OS-native scheduled notifications |
| 🎮 **Game Updates** | Check and trigger supported game updates |
| 📱 **Remote Dashboard** | Browser-based dashboard with QR pairing |
| 🔌 **Multi-provider AI** | Google, NVIDIA, Anthropic, OpenAI-compatible and local provider support |

---

## 🧠 Architecture

MARK LII is organized as a layered assistant rather than a single monolithic chatbot.

```text
                         ┌─────────────────────┐
                         │      User Input     │
                         │  Voice / Keyboard   │
                         └──────────┬──────────┘
                                    │
                                    ▼
                    ┌──────────────────────────────┐
                    │       MARK LII HUD / UI      │
                    │ PyQt6 · Voice · Visual HUD   │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
                    ┌──────────────────────────────┐
                    │        JarvisCore             │
                    │ Routing · Context · Safety    │
                    │ Planning · Verification       │
                    └──────────────┬───────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
              ▼                    ▼                    ▼
       ┌─────────────┐     ┌──────────────┐     ┌──────────────┐
       │ AI Gateway  │     │ Agent Layer  │     │ Memory Layer │
       │ Providers   │     │ Planner      │     │ Long-term    │
       │ Routing     │     │ Orchestrator │     │ Episodic     │
       └──────┬──────┘     └──────┬───────┘     └──────┬───────┘
              │                    │                    │
              └────────────────────┼────────────────────┘
                                   │
                                   ▼
                       ┌────────────────────────┐
                       │      Tool / Action      │
                       │ File · Browser · OS     │
                       │ Web · Weather · Voice   │
                       │ Vision · Reminders      │
                       └───────────┬────────────┘
                                   │
                                   ▼
                         ┌───────────────────┐
                         │ External Systems  │
                         │ OS / Web / APIs   │
                         └───────────────────┘
```

---

## 🛠️ Technology Stack

### Core

- **Python**
- **PyQt6**
- **Google Gemini / Gemini Live API**
- **NumPy**
- **OpenCV**
- **Pillow**
- **sounddevice**
- **mss**
- **psutil**

### AI & Agent Infrastructure

- Gemini Live
- Multi-provider AI gateway
- Model routing and fallback
- Agent planner
- Agent orchestrator
- Vision agent
- Computer-use agent
- Code agent
- Research workflows
- Safety and permission controls
- Model health tracking

### Automation & Desktop Control

- PyAutoGUI
- PyGetWindow
- PyWin32 / PyWinAuto on Windows
- pycaw for Windows audio control
- OS-native reminders
- Browser automation through Playwright

### Web & Data

- Requests
- BeautifulSoup
- DDGS
- FastAPI
- Uvicorn
- YouTube Transcript API

### Security & Storage

- Cryptography
- Local configuration and memory stores
- Secret handling and redaction
- Permission / confirmation gates
- Audit and recovery modules

---

## 📁 Project Structure

```text
Mark-LII/
│
├── main.py                    # Main application entry point
├── setup.py                   # Dependency + Playwright setup
├── requirements.txt           # Python dependencies
├── .env.example               # Environment variable template
│
├── actions/                   # Concrete assistant capabilities
│   ├── browser_control.py
│   ├── code_helper.py
│   ├── computer_control.py
│   ├── computer_settings.py
│   ├── file_controller.py
│   ├── file_processor.py
│   ├── flight_finder.py
│   ├── open_app.py
│   ├── reminder.py
│   ├── send_message.py
│   ├── system_monitor.py
│   ├── weather_report.py
│   ├── web_search.py
│   ├── youtube_video.py
│   └── ...
│
├── core/                     # AI, agent, safety and runtime architecture
│   ├── jarvis_core.py
│   ├── ai_gateway.py
│   ├── ai_provider.py
│   ├── model_router.py
│   ├── model_registry.py
│   ├── agents/
│   ├── providers/
│   ├── tasks/
│   ├── voice.py
│   ├── vision.py
│   ├── memory/
│   ├── safety.py
│   ├── permissions.py
│   ├── verification.py
│   ├── computer_agent.py
│   ├── browser_agent.py
│   └── ...
│
├── memory/                   # Persistent and episodic memory
│   ├── memory_manager.py
│   ├── memory_hub.py
│   ├── episodic.py
│   ├── identity.py
│   ├── capture.py
│   └── policy.py
│
├── plugins/                  # Drop-in extensions
│   ├── _template.py
│   ├── calculator.py
│   ├── currency_converter.py
│   └── unit_converter.py
│
├── dashboard/                # Remote browser dashboard
│   ├── server.py
│   ├── guard.py
│   └── static/
│
├── config/                   # Runtime, model and persona configuration
│   ├── models.json
│   ├── persona.json
│   └── runtime.json
│
├── tools/                    # Diagnostics, benchmarks and probes
│
├── tests/                    # Automated test suite
│
├── docs/                     # Architecture and audit documentation
│
├── ui.py                     # Main PyQt6 interface
├── ui_holo.py                # Holographic UI
├── ui_command_center.py      # Command center UI
└── ui_mission.py             # Mission interface
```

---

# 🚀 Getting Started

## 1. Clone the repository

```bash
git clone https://github.com/<your-username>/<your-repository>.git
cd <your-repository>
```

## 2. Create a virtual environment

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## 3. Install dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Install the Playwright browser runtime:

```bash
python -m playwright install
```

Or use the project's setup script:

```bash
python setup.py
```

## 4. Configure your API keys

Copy the environment template:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

At minimum, configure:

```env
GOOGLE_API_KEY=your-google-api-key
```

The project also supports optional provider configuration:

```env
NVIDIA_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

JARVIS_AI_PROVIDER=google
JARVIS_AI_MODEL=
JARVIS_AI_MODEL_FAST=
JARVIS_AI_FALLBACKS=
```

> **Never commit `.env` or real API keys to GitHub.**

## 5. Start MARK LII

```bash
python main.py
```

On Windows, the repository also includes:

```text
Start_JARVIS.bat
```

which can be used as a convenient launcher.

---

# 🎙️ Voice Interaction

MARK LII uses a real-time audio pipeline for conversational interaction.

The runtime includes:

- Microphone capture
- Speech input
- Gemini Live transport
- Streaming audio output
- Voice interruption / barge-in handling
- Stop-phrase detection
- Audio-level visualization
- Session resumption
- Configurable voice selection

The configuration can be adjusted in:

```text
config/runtime.json
config/models.json
```

---

# 🧠 Memory System

MARK LII separates **stored memory** from the amount of memory included in each model prompt.

The memory architecture includes:

```text
User conversation
       │
       ▼
Memory intent detection
       │
       ▼
Memory capture / policy
       │
       ▼
Long-term + episodic storage
       │
       ▼
Relevant-memory retrieval
       │
       ▼
AI context
```

Memory functionality includes:

- Persistent user facts
- Project context
- Preferences
- Session summaries
- Local retrieval
- Memory inspection through the UI
- Selective forgetting
- Context budgeting

The design avoids treating the entire memory store as a giant prompt on every request.

---

# ↩️ Undo & Safety

Because MARK LII can interact with the operating system, the project includes safeguards around potentially destructive operations.

### Supported undo categories

Depending on the operation, MARK LII can reverse actions such as:

- File moves
- File renames
- File creation
- File copies
- File writes
- Desktop organization
- Selected system setting changes

### Confirmation gates

Certain irreversible operations require explicit user confirmation from the interface.

This is intentionally different from allowing the language model to provide its own confirmation flag.

Relevant modules include:

```text
core/undo.py
core/confirm.py
core/safety.py
core/permissions.py
core/risk.py
core/verification.py
```

---

# 🧩 Plugin System

MARK LII is designed to be extended without modifying the core assistant.

Start with:

```text
plugins/_template.py
```

Then add your plugin to:

```text
plugins/
```

Example:

```text
plugins/
├── _template.py
├── calculator.py
├── currency_converter.py
├── unit_converter.py
└── my_plugin.py
```

The plugin loader discovers available extensions during startup.

This keeps new capabilities isolated from the main application.

---

# 🌐 Web Search

The web search action supports several modes:

```text
search
news
research
price
compare
```

The assistant can therefore handle requests such as:

```text
"Search for the latest AI security news."

"Research the current state of browser agents."

"Compare these two laptops."

"Find the current price of this product."
```

Search functionality is implemented primarily through the project's web-search layer and configured AI providers.

---

# 👁️ Vision

MARK LII can work with visual input from:

- Desktop screenshots
- Screen streams
- Webcam/camera input

The vision pipeline is connected to the main assistant rather than being a completely separate chatbot.

Relevant modules include:

```text
core/vision.py
core/screen_watch.py
actions/screen_processor.py
core/computer_agent.py
```

---

# 🖥️ Computer Control

The action layer exposes operating-system capabilities such as:

```text
Open applications
Control windows
Keyboard shortcuts
Mouse interaction
Volume control
Brightness control
Wi-Fi control
Power operations
Browser interaction
File management
Desktop organization
```

Computer actions are routed through the assistant's tool and safety layers rather than being directly executed by the language model.

---

# 📊 Hardware Monitoring

MARK LII can expose live system telemetry including:

- CPU utilization
- RAM utilization
- GPU information where available
- CPU temperature where supported
- Uptime
- Process information

Primary implementation:

```text
actions/system_monitor.py
```

---

# 📱 Remote Dashboard

The project contains a FastAPI-based dashboard:

```text
dashboard/server.py
```

with browser-facing assets under:

```text
dashboard/static/
```

The dashboard can be used as a remote control surface for the assistant, including QR-based pairing functionality.

Because the dashboard exposes control functionality, it should only be deployed in environments you trust.

---

# ⚙️ Configuration

Important configuration files:

| File | Purpose |
|---|---|
| `config/models.json` | AI model tiers, providers, aliases and fallback configuration |
| `config/persona.json` | Assistant persona configuration |
| `config/runtime.json` | Runtime, voice, web-development, proactive and vision settings |
| `.env` | Secrets and provider configuration |
| `core/prompt.txt` | Main assistant prompt |
| `core/personality_serious.txt` | Serious personality mode |
| `core/personality_friendly.txt` | Friendly personality mode |

Example runtime configuration:

```json
{
  "tasks": {
    "max_parallel": 3,
    "default_tool_timeout": 300
  },
  "proactive": {
    "enabled": true,
    "max_per_hour": 2
  },
  "voice": {
    "tts_provider": "gemini_live",
    "tool_profile": "full"
  }
}
```

---

# 🧪 Testing

The repository contains tests covering major architectural areas:

```text
tests/
├── test_architecture.py
├── test_extensions.py
├── test_main_smoke.py
├── test_providers.py
├── test_phase5_cognition.py
├── test_phase5_memory.py
├── test_phase5_plugins_vault.py
├── test_phase5_safety.py
├── test_phase6_agents.py
├── test_phase6_integration.py
├── test_phase6_models.py
├── test_phase6_tasks.py
├── test_phase6_webdev.py
├── test_phase7_actions.py
├── test_phase7_pocket.py
├── test_phase7_providers.py
├── test_phase7_security.py
├── test_phase7_voice.py
├── test_ui_holo.py
└── ...
```

Run the test suite with:

```bash
python -m pytest -q
```

For individual areas:

```bash
python -m pytest tests/test_architecture.py -q
python -m pytest tests/test_providers.py -q
python -m pytest tests/test_phase7_voice.py -q
```

The `tools/` directory also contains diagnostic and benchmarking utilities for providers, latency, UI rendering, voice behavior and live-tool integration.

---

# 🔐 Security Notes

MARK LII is a **local computer-control application**. Treat it with the same care you would give any automation software capable of interacting with your operating system.

### Before running it

- Keep API keys in `.env`, never in source control.
- Review the permissions granted to the application.
- Do not run untrusted plugins.
- Review browser automation targets before allowing actions.
- Be careful when enabling power, network or file-management actions.
- Keep the remote dashboard inaccessible to untrusted networks.
- Review configuration changes before using the assistant on a production machine.

### Secrets

The repository includes:

```text
.env.example
```

but real credentials should remain in:

```text
.env
```

and should never be committed.

---

# 🗺️ Release Lineage

MARK LII builds on a series of earlier assistant iterations:

| Release | Focus |
|---|---|
| **XLVIII** | Interrupt handling, parallel news, briefing improvements, retry logic |
| **XLIX** | Auto-start, clipboard intelligence, assistant customization |
| **L** | Session memory, background monitoring, proactive behavior |
| **LI** | Plugin system, affective dialog, proactive audio, unlimited sessions |
| **LII** | Voice picker, live theming, reactive HUD |

MARK LII therefore represents a cumulative assistant architecture rather than a standalone prototype.

---

# 🧭 Roadmap Ideas

Potential extensions for future releases include:

- 📧 Email plugin
- 📅 Calendar integration
- 🧑‍💻 Expanded coding-agent workflows
- 🔌 Larger plugin marketplace
- 🏠 Smart-home integrations
- 📱 Expanded mobile control
- 🧠 More advanced long-term memory retrieval
- 🔐 Additional permission policies
- 🧪 Expanded end-to-end testing
- ⚡ Further voice-latency optimization

---

# 🤝 Contributing

Contributions are welcome.

A typical workflow:

```bash
git checkout -b feature/my-feature
```

Make your changes, add or update tests, then run:

```bash
python -m pytest -q
```

Commit your work:

```bash
git add .
git commit -m "feat: add my feature"
git push origin feature/my-feature
```

Then open a pull request.

For new assistant capabilities, prefer adding a **plugin or action module** rather than modifying the central runtime whenever practical.

---

# 📄 License

This repository includes a `LICENSE` file. See that file for the exact licensing terms applicable to the project.

---

# ⭐ Why MARK LII?

MARK LII is designed around a simple idea:

> **A personal AI assistant should be able to perceive, remember, reason, act, verify, and recover.**

Instead of treating an LLM as the entire application, MARK LII wraps the model in a larger system of:

**voice + memory + agents + tools + vision + safety + verification + UI + plugins**

That architecture turns a conversational model into a practical desktop assistant platform.

---

## 🛡️ Built for experimentation

MARK LII is especially useful as a platform for experimenting with:

- Personal AI assistants
- Agentic AI
- Voice interfaces
- Computer-use agents
- Multimodal AI
- Long-term memory
- AI safety and permission systems
- Local automation
- AI tool orchestration
- Multi-provider model routing

If you are studying or building in **AI, data science, automation, cybersecurity, or intelligent agents**, the codebase provides several independent systems to explore.

---

**MARK LII • Personal AI Assistant Platform**
