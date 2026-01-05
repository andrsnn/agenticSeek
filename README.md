# AgenticSeek Enhanced Fork

<p align="center">
<img align="center" src="./media/agentic_seek_logo.png" width="300" height="300" alt="Agentic Seek Logo">
</p>

> **This is an enhanced fork of [AgenticSeek](https://github.com/Fosowl/agenticSeek) by [Fosowl](https://github.com/Fosowl).**
> All credit for the original concept, architecture, and core functionality goes to the original author and contributors.

---

## What is AgenticSeek?

**AgenticSeek is a 100% local AI assistant that can browse the web, write code, and plan complex tasks — all while keeping your data on your device.**

Think of it like having a personal AI researcher that can:
- 🌐 **Browse the web** — Search Google, read articles, fill forms, click buttons
- 💻 **Write & run code** — Python, JavaScript, Go, and more
- 📋 **Plan multi-step tasks** — Break down complex requests into actionable steps
- 🔒 **Stay private** — Everything runs locally, no cloud, no data sharing

**Example:** *"Search the web for wellness strategies for psoriasis, find what Bryan Johnson recommends, and write me a report"* — and it just... does it.

---

## Screenshots

<p align="center">
<img src="./screenshots/Screenshot 2026-01-02 101201.png" alt="AgenticSeek UI - Sources Panel" width="100%">
</p>

<p align="center">
<img src="./screenshots/Screenshot 2026-01-02 103047.png" alt="AgenticSeek UI - Report Generation" width="100%">
</p>

---

## Why This Fork?

The original AgenticSeek is a fantastic project. This fork builds on it with quality-of-life improvements, better reliability, and new features that make it more usable day-to-day.

**This fork is provided as-is, open source, with no guarantees.** But if you want a version that (in my experience) works more reliably out of the box, this might be for you.

### What's Different?

| Feature | Description |
|---------|-------------|
| **📚 Sources Panel** | Automatically captures every webpage visited with summaries, relevancy scores, and verbatim excerpts. Grouped by domain for easy navigation. |
| **✏️ Mid-Run Amendments** | Add notes to a running task without restarting. Say "what about coffee?" and it gets incorporated into the current work. |
| **📝 Smart File Output** | New `write_output` tool handles CSV, Markdown, and text files reliably. No more path issues on Windows. |
| **🔄 Improved Verifier** | 6 retries with progress tracking. Only retries if actually improving. Smarter content sampling for long outputs. |
| **📊 Report Generation** | Generate comprehensive markdown reports from collected sources with one click. |
| **⚙️ Better Settings** | Explanations for every setting. Sensible defaults. Toggle source collection on/off. |
| **🪟 Windows Support** | Refined PowerShell script that actually works. Simple `.\start_services.ps1` to start. |
| **🎯 Better Routing** | Improved detection of when to use the Planner vs. simple agents. |

### Key Improvements

**Sources & Research:**
- Every page the AI visits is logged with URL, title, summary, and relevancy score
- Sources grouped by domain with collapsible sections
- Export sources to markdown
- Generate reports from collected research

**Reliability:**
- Verifier tracks progress and stops retrying if not improving
- Smart sampling of long outputs so verifier sees the whole picture
- Better error handling and logging throughout
- File operations contained to run folders (no accidental writes elsewhere)

**User Experience:**
- Add amendments mid-task with the purple + button
- Settings have explanations so you know what each toggle does
- Activity feed shows what's happening in real-time
- Tab descriptions explain what each panel is for

---

## Quick Start

### Prerequisites

- **Docker Desktop** — [Download here](https://docs.docker.com/desktop/install/windows-install/)
- **Git** — [Download here](https://git-scm.com/downloads)

### 1. Clone and Setup

```bash
git clone https://github.com/YOUR_USERNAME/agenticSeek.git
cd agenticSeek
copy .env.example .env
```

### 2. Configure `.env`

Edit `.env` with your settings:

```env
SEARXNG_BASE_URL="http://searxng:8080"
REDIS_BASE_URL="redis://redis:6379/0"
WORK_DIR="C:/Users/YourName/Documents/ai_workspace"
OLLAMA_PORT="11434"

# API keys are OPTIONAL - only needed if using cloud LLMs
OPENAI_API_KEY='optional'
DEEPSEEK_API_KEY='optional'
```

Set `WORK_DIR` to a folder where the AI can read/write files.

### 3. Start Services

**Windows (PowerShell):**
```powershell
.\start_services.ps1
```

**macOS / Linux:**
```bash
./start_services.sh full
```

**First run takes 10-30 minutes** to download Docker images. Wait until you see `backend: "GET /health HTTP/1.1" 200 OK` in logs.

### 4. Open the UI

Go to **http://localhost:3000** in your browser.

---

## Usage

### Basic Commands

Just type what you want in natural language:

> *"Search the web for the best cafes in Paris and save a list to cafes.txt"*

> *"Write a Python script that calculates compound interest"*

> *"Find information about climate change solutions and write me a report"*

### Using Amendments

While a task is running, you can add notes without restarting:

1. Type your addition in the chat box
2. Click the **purple + button** (appears while task is running)
3. Your note gets incorporated into the current step

Example: Task is researching wellness strategies, you realize you forgot something:
> Click + → *"also include information about sleep quality"*

### PowerShell Commands

```powershell
.\start_services.ps1           # Start all services
.\start_services.ps1 stop      # Stop services
.\start_services.ps1 restart   # Restart (picks up code changes)
.\start_services.ps1 logs      # Follow all logs
.\start_services.ps1 logs backend   # Follow backend only
.\start_services.ps1 status    # Show container status
```

---

## Running LLMs Locally

To run completely locally (no cloud APIs), you need a local LLM provider like **Ollama**.

### Hardware Requirements

| Model Size | GPU VRAM | Experience |
|------------|----------|------------|
| 7B | 8GB | ⚠️ Struggles with complex tasks |
| 14B | 12GB (RTX 3060) | ✅ Good for simple tasks |
| 32B | 24GB (RTX 4090) | 🚀 Great for most tasks |
| 70B+ | 48GB+ | 💪 Excellent for everything |

### Setup with Ollama

1. Install [Ollama](https://ollama.ai)
2. Pull a model: `ollama pull deepseek-r1:14b`
3. Start Ollama: `ollama serve`
4. Update `config.ini`:

```ini
[MAIN]
is_local = True
provider_name = ollama
provider_model = deepseek-r1:14b
provider_server_address = 127.0.0.1:11434
```

---

## Using Cloud APIs (Optional)

If you don't have GPU hardware, you can use cloud LLM providers.

1. Get an API key from your provider (OpenAI, Google, DeepSeek, etc.)
2. Add to `.env`: `OPENAI_API_KEY='your-key-here'`
3. Update `config.ini`:

```ini
[MAIN]
is_local = False
provider_name = openai
provider_model = gpt-4o
```

---

## Configuration

### config.ini

```ini
[MAIN]
is_local = True                    # True for local LLMs, False for cloud APIs
provider_name = ollama             # ollama, lm-studio, openai, google, etc.
provider_model = deepseek-r1:14b   # Model name
provider_server_address = 127.0.0.1:11434
agent_name = Friday                # Your AI's name
recover_last_session = False
save_session = False
speak = False                      # Text-to-speech output
listen = False                     # Speech-to-text input (CLI only)

[BROWSER]
headless_browser = True            # False to see the browser window
stealth_mode = True                # Reduce bot detection
```

### Frontend Settings

In the UI (gear icon), you can configure:

- **Default output format** — None, Markdown, or CSV
- **Source collection** — Toggle automatic source capture
- **LLM enrichment** — Toggle AI-powered source summaries
- **Enabled agents** — Control which agents can be used
- **Enabled tools** — Control which tools agents can use

---

## Troubleshooting

### Backend won't start

```powershell
docker compose logs backend
```

Look for error messages. Common issues:
- `.env` file missing → `copy .env.example .env`
- Docker not running → Start Docker Desktop
- Port conflict → Check if 7777 or 3000 are in use

### Browser issues

If you see ChromeDriver errors, you may need to download a matching ChromeDriver version. See the [ChromeDriver section](#chromedriver-issues) below.

### LLM not responding

- For Ollama: Make sure `ollama serve` is running
- For cloud APIs: Check your API key is set in `.env`
- Check `config.ini` matches your setup

---

## ChromeDriver Issues

If you see version mismatch errors:

1. Check your Chrome version: `chrome://settings/help`
2. Download matching ChromeDriver from [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)
3. Place `chromedriver.exe` in the project root

---

## Credits & Attribution

**Original Project:** [AgenticSeek by Fosowl](https://github.com/Fosowl/agenticSeek)

This fork would not exist without the excellent foundation built by:
- **[Fosowl](https://github.com/Fosowl)** — Original creator
- **[antoineVIVIES](https://github.com/antoineVIVIES)** — Maintainer
- **[tcsenpai](https://github.com/tcsenpai)** and **[plitc](https://github.com/plitc)** — Docker contributions
- All the open-source contributors

**Please star the original repo:** ⭐ https://github.com/Fosowl/agenticSeek

---

## License

This project is licensed under GPL-3.0, same as the original.

---

## Disclaimer

This is a personal fork with modifications that work for my use case. It's provided as-is with no guarantees of functionality, support, or maintenance. Use at your own risk.

For the official, supported version, please use the [original AgenticSeek](https://github.com/Fosowl/agenticSeek).
