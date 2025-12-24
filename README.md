# telegram-autoregexbot

Telegram bot that performs configurable regex-based message substitutions using sed/perl syntax.
originally made to change x links to fxtwitter.

## Features
*   **Regex Substitutions:** Supports sed-style `s/pattern/replacement/flags`.
*   **Access Control:** Whitelist/Blacklist for Chats and Users.
*   **Message Handling:** Reply, Mention original user, Cooldowns.
*   **Delete Button:** Configurable permissions (Sender, Admin, etc.).
*   **Privacy:** Secrets separated from logic.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/yourusername/telegram-autoregexbot.git
    cd telegram-autoregexbot
    ```

2.  **Install Dependencies:**
    ```bash
    pip install .
    ```
    *Or for development:*
    ```bash
    pip install -e .
    ```

3.  **Configuration:**
    *   Rename `secrets.cfg.example` to `secrets.cfg` and add your Telegram Bot Token.
    *   Edit `autoregexbot.cfg` to define your Regex rules and access policies.

## Running the Bot

### **Linux / MacOS**
You can run the installed script directly:
```bash
telegram-autoregexbot
```