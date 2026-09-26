# SofiCard-Sender

A configurable Discord bulk card sender with persistent progress tracking, randomized delays, pause/resume controls, and dry-run support.

> **Note:** This project is intended for authorized automation and personal workflows. Use it only where automation is permitted by the relevant service and server rules.

---

## ✨ Features

- 📋 Reads codes from `data/remaining.txt`
- 📝 Automatically tracks processed codes in `data/sent.txt`
- 💾 Persists progress so interrupted runs can resume
- ⏱️ Configurable delays and cooldowns
- ⏸️ F8 — Pause / Resume
- 🛑 F9 — Emergency stop
- 🧪 Dry-run mode for testing without sending commands
- 🖱️ PyAutoGUI failsafe support
- 📋 Automatically restores the previous clipboard contents
- 🔒 Prevents multiple instances from running simultaneously
- 🎯 Optional Discord-window focus verification
- 📊 Runtime progress and ETA information
- ⚙️ Configuration through `config.json`
- 📝 Rotating runtime log

---

## 📋 Requirements

### Operating System

The project is primarily designed for desktop environments where PyAutoGUI can control the keyboard and clipboard.

### Python

Python 3.10+ is recommended.

### Python packages

Install the required packages with:

```bash
pip install -r requirements.txt
```

The dependencies are:

- PyAutoGUI
- Pyperclip
- keyboard
- PyGetWindow

---

## 📦 Installation

### 1. Clone the repository

```bash
git clone https://github.com/JhunJhun-chan/SofiCard-Sender.git
```

Enter the project directory:

```bash
cd SofiCard-Sender
```

### 2. Create a virtual environment

Windows:

```bash
python -m venv .venv
```

Activate it:

```bash
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

---

## ⚙️ Configuration

The program can be configured through:

```text
config.json
```

Example:

```json
{
  "startup_delay": 10.0,

  "pre_paste_min": 4.0,
  "pre_paste_max": 7.0,
  "post_paste_min": 3.0,
  "post_paste_max": 6.0,

  "channel_cooldown": 10.0,
  "cooldown_jitter_min": 2.0,
  "cooldown_jitter_max": 7.0,

  "think_chance": 0.25,
  "think_min": 3.0,
  "think_max": 10.0,

  "break_every_min": 8,
  "break_every_max": 15,
  "break_duration_min": 30.0,
  "break_duration_max": 90.0,

  "review_chance": 0.15,
  "review_min": 1.5,
  "review_max": 4.0,

  "log_max_lines": 500,
  "clear_log_on_exit": true,

  "require_discord_focus": true,
  "restore_clipboard": true,

  "code_pattern": "[a-zA-Z0-9]{5,8}"
}
```

All configuration values are optional. Missing values fall back to the program defaults.

---

## 📁 Project Structure

```text
SofiCard-Sender/
│
├── data/
│   └── .gitkeep
│
├── .gitignore
├── LICENSE
├── README.md
├── config.json
├── main.py
└── requirements.txt
```

### Runtime files

The program automatically creates and manages:

```text
data/
├── sent.txt
├── remaining.txt
├── log.txt
└── .lock
```

These files are intentionally excluded from Git.

### `sent.txt`

Contains codes that have already been processed.

### `remaining.txt`

Contains codes that are still waiting to be processed.

### `log.txt`

Contains runtime information and recent activity.

### `.lock`

Prevents multiple instances of the program from running simultaneously.

---

## ▶️ Usage

Start the program with:

```bash
python main.py
```

The program will:

1. Load the configuration.
2. Load previously processed codes.
3. Load codes from `data/remaining.txt`.
4. If no remaining codes exist, prompt for input.
5. Validate and deduplicate the supplied codes.
6. Wait for the configured startup delay.
7. Wait for the Discord window to be focused.
8. Process each code.
9. Persist progress after every successful operation.

---

## 📋 Input

If `data/remaining.txt` is empty or does not exist, the program can prompt for codes.

Codes are parsed from whitespace- or comma-separated input.

The default validation pattern is:

```text
[a-zA-Z0-9]{5,8}
```

Already processed codes are automatically excluded.

Duplicate codes in the same input are also removed.

---

## 🧪 Dry Run

Before performing any actions, you can test the input parser with:

```bash
python main.py --dry-run
```

Dry-run mode:

- Loads the codes
- Validates them
- Removes duplicates
- Excludes previously processed codes
- Prints what would be processed
- Does not send commands

This is recommended when testing a new input set or configuration.

---

## ⏸️ Keyboard Controls

| Key | Action |
|---|---|
| `F8` | Pause / Resume |
| `F9` | Emergency stop |

### PyAutoGUI failsafe

PyAutoGUI's failsafe is enabled.

Moving the mouse cursor to a screen corner can trigger the failsafe and stop the program.

---

## 🛡️ Safety Features

### Discord focus verification

When enabled:

```json
"require_discord_focus": true
```

the program checks that the active window appears to be Discord before processing a code.

If Discord is not focused, processing pauses until the correct window is active.

### Clipboard restoration

When enabled:

```json
"restore_clipboard": true
```

the program saves the existing clipboard contents before preparing a command and attempts to restore them afterward.

### Instance lock

The program creates a lock file while running to prevent multiple instances from operating on the same data simultaneously.

---

## 💾 Progress Persistence

Progress is saved after each processed code.

This means an interrupted run does not require starting from the beginning.

The program maintains two separate lists:

```text
sent.txt
remaining.txt
```

After successfully processing a code:

1. The code is added to `sent.txt`.
2. The code is removed from `remaining.txt`.
3. The updated remaining list is saved.

---

## ⏱️ Timing Configuration

The program supports configurable timing values including:

- Startup delay
- Pre-paste delay
- Post-paste delay
- Channel cooldown
- Cooldown jitter
- Optional thinking pauses
- Periodic longer breaks

For example:

```json
"channel_cooldown": 10.0,
"cooldown_jitter_min": 2.0,
"cooldown_jitter_max": 7.0
```

These values control the delay between processing operations.

Always use timings appropriate for the service and workflow in which you are operating.

---

## 📝 Logging

Runtime information is written to:

```text
data/log.txt
```

The maximum number of retained log lines can be configured with:

```json
"log_max_lines": 500
```

You can also control whether the runtime log is cleared after a clean shutdown:

```json
"clear_log_on_exit": true
```

---

## 🔧 Command-Line Options

### Normal execution

```bash
python main.py
```

### Custom configuration file

```bash
python main.py --config my-config.json
```

### Dry run

```bash
python main.py --dry-run
```

### Combine options

```bash
python main.py --config my-config.json --dry-run
```

---

## 🐛 Troubleshooting

### `ModuleNotFoundError`

Install the dependencies:

```bash
pip install -r requirements.txt
```

If you are using a virtual environment, make sure it is activated first.

---

### F8/F9 do not work

The keyboard-control package may not be installed or may not have the required permissions.

Try:

```bash
pip install keyboard
```

The program can still operate without the keyboard package, but F8/F9 controls will be disabled.

---

### Discord is not detected

Make sure the Discord desktop application is open and focused.

You can also check:

```json
"require_discord_focus": true
```

If focus detection is not required for your environment, it can be disabled:

```json
"require_discord_focus": false
```

---

### Program stops unexpectedly

Check:

```text
data/log.txt
```

The log can contain information about the last operation performed.

Also remember that moving the mouse to a PyAutoGUI failsafe corner can stop execution.

---

### Multiple-instance error

If the program reports that another instance is already running, make sure another copy of the program is not active.

If a previous run crashed and left a stale lock file, verify that no instance is running before removing:

```text
data/.lock
```

---

## ⚠️ Important Notes

This software controls the local keyboard, clipboard, and active window.

Because it performs GUI automation:

- Keep the Discord window available while running.
- Do not interact with the keyboard/mouse unexpectedly during processing.
- Test new configurations with `--dry-run` first.
- Keep runtime data out of version control.
- Use the software only where automated interaction is permitted.

---

## 🤝 Contributing

Pull requests and issue reports are welcome.

When submitting a bug report, include:

- Operating system
- Python version
- Installation method
- Relevant configuration
- Error message
- Relevant log output

Do **not** include private codes, credentials, tokens, or personal information.

---

## 📜 License

This project is licensed under the MIT License.

See [`LICENSE`](LICENSE) for details.

---

## ⭐ Support

If you find the project useful, consider giving the repository a star on GitHub.
