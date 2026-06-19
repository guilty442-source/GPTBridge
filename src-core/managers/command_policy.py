from enum import Enum

class CommandRisk(str, Enum):
    SAFE = "SAFE"
    CONFIRM = "CONFIRM"
    BLOCKED = "BLOCKED"

class CommandPolicy:
    SAFE_PREFIXES = [
        "python -m py_compile",
        "npm.cmd run dev",
        "npm.cmd run build",
        "npm run dev",
        "npm run build",
        "pytest",
        "playwright install",
        "git status",
        "git diff",
    ]
    
    BLOCKED_PREFIXES = [
        "format",
        "rm -rf",
        "del /s",
        "rd /s",
        "rmdir /s",
        "mkfs",
        "dd if=",
        "remove-item",
        "recursive destructive delete",
        "arbitrary shell execution outside project"
    ]

    CONFIRM_PREFIXES = [
        "pip install",
        "npm install",
        "npm.cmd install",
        "file delete",
        "cleanup apply",
        "cleanup actions"
    ]

    @classmethod
    def evaluate(cls, command: str) -> CommandRisk:
        cmd_lower = command.lower().strip()
        for blocked in cls.BLOCKED_PREFIXES:
            if cmd_lower.startswith(blocked):
                return CommandRisk.BLOCKED
                
        for safe in cls.SAFE_PREFIXES:
            if cmd_lower.startswith(safe):
                return CommandRisk.SAFE
                
        for confirm in cls.CONFIRM_PREFIXES:
            if cmd_lower.startswith(confirm):
                return CommandRisk.CONFIRM

        return CommandRisk.CONFIRM
