# File: main.py
import asyncio
from CORE.bot import start_app

if __name__ == "__main__":
    try:
        asyncio.run(start_app())
    except KeyboardInterrupt:
        print("\nProcess terminated by user.")


# # chmod 600 ssh_key.txt
# # eval "$(ssh-agent -s)" 
# # ssh-add ssh_key.txt
# # source .ssh-autostart.sh
# # git push --set-upstream origin master
# # git config --global push.autoSetupRemote true
# # ssh -T git@github.com 
# # git log -1

# # git add .
# # git commit -m "plh37"
# # git push

# # pip install anthropic
# # npm install -g @anthropic-ai/claude-code

# # export ANTHROPIC_API_KEY=...
# # claude

# taskkill /F /IM python.exe