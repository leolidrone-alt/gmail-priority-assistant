"""初始配置窗口（Tkinter）。

用途：第一次使用或换 key 时，用一个长方形小窗口填 Gmail 地址和 DeepSeek API Key。
两栏都留空直接回车 = 使用 .env 里现有的值（首次即你给的那个 key 和邮箱）。

界面文案为英文（UI language: English）。

运行：
    .venv\\Scripts\\python.exe setup_gui.py
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

import settings

BG = "#eef1f5"
CARD = "#ffffff"
TEXT = "#1f2937"
MUTED = "#6b7280"
BORDER = "#d1d5db"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ERROR = "#dc2626"
WARN = "#b45309"
SUCCESS = "#059669"

FONT = "Segoe UI"

WIDTH, HEIGHT = 560, 430


class SetupApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.defaults = settings.load_defaults()
        self.result: dict | None = None
        self._busy = False

        root.title("Mail Assistant · Setup")
        root.geometry(f"{WIDTH}x{HEIGHT}")
        root.resizable(False, False)
        root.configure(bg=BG)
        self._center(WIDTH, HEIGHT)

        tkfont.nametofont("TkDefaultFont").configure(family=FONT, size=10)
        tkfont.nametofont("TkTextFont").configure(family=FONT, size=10)

        card = tk.Frame(root, bg=CARD)
        card.pack(fill="both", expand=True, padx=18, pady=18)

        tk.Label(card, text="Mail Assistant · Setup", bg=CARD, fg=TEXT,
                 font=(FONT, 15, "bold")).pack(anchor="w", padx=24, pady=(20, 4))
        tk.Label(card, text="Leave both fields blank and press Enter to use the defaults.",
                 bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w", padx=24)

        # --- Gmail ---------------------------------------------------
        tk.Label(card, text="Gmail address", bg=CARD, fg=TEXT).pack(anchor="w", padx=24, pady=(18, 4))
        self.email_var = tk.StringVar()
        self.email_entry = self._entry(card, self.email_var)
        self.email_entry.bind("<FocusOut>", self._check_email_quietly)
        tk.Label(card, text=f"Default: {self.defaults['email'] or 'not set'}", bg=CARD, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="w", padx=24, pady=(3, 0))

        # --- API key -------------------------------------------------
        tk.Label(card, text="DeepSeek API key", bg=CARD, fg=TEXT).pack(anchor="w", padx=24, pady=(14, 4))
        self.key_var = tk.StringVar()
        self.key_entry = self._entry(card, self.key_var, show="*")
        self.show_var = tk.BooleanVar(value=False)
        self.show_btn = tk.Button(
            card, text="Show key", command=self._toggle_key,
            bg=CARD, fg=ACCENT, activebackground=CARD, activeforeground=ACCENT_DARK,
            relief="flat", bd=0, cursor="hand2", font=(FONT, 9, "underline"),
        )
        self.show_btn.pack(anchor="w", padx=20, pady=(4, 0))
        tk.Label(card, text=f"Default: {self.defaults['key_masked']}", bg=CARD, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="w", padx=24, pady=(2, 0))

        # --- status + buttons ----------------------------------------
        self.status = tk.Label(card, text="", bg=CARD, fg=MUTED, justify="left",
                               wraplength=470, font=(FONT, 9))
        self.status.pack(anchor="w", padx=24, pady=(14, 0))

        tk.Frame(card, height=1, bg="#e5e7eb").pack(fill="x", padx=24, pady=(10, 0))

        footer = tk.Frame(card, bg=CARD)
        footer.pack(side="bottom", fill="x", padx=24, pady=(12, 18))

        tk.Label(footer, text="Enter = save and continue    ·    Esc = cancel", bg=CARD, fg=MUTED,
                 font=(FONT, 8)).pack(side="left")

        self.cancel_btn = tk.Button(footer, text="Cancel", command=self._cancel, width=9,
                                    bg=CARD, fg=TEXT, activebackground="#e5e7eb",
                                    relief="solid", bd=1, cursor="hand2")
        self.cancel_btn.pack(side="right")

        self.save_btn = tk.Button(footer, text="Save and continue", command=self._submit, width=17,
                                  bg=ACCENT, fg="white", activebackground=ACCENT_DARK,
                                  activeforeground="white", disabledforeground="#e5e7eb",
                                  relief="flat", cursor="hand2",
                                  font=(FONT, 10, "bold"))
        self.save_btn.pack(side="right", padx=(0, 10))

        root.bind("<Return>", lambda _event: self._submit())
        root.bind("<Escape>", lambda _event: self._cancel())
        root.protocol("WM_DELETE_WINDOW", self._cancel)

        # 按内容自适应高度，避免底部按钮被裁掉（不同 DPI/字体下高度会变）
        root.update_idletasks()
        height = max(HEIGHT, card.winfo_reqheight() + 36)
        if height != HEIGHT:
            self._center(WIDTH, height)

        self.email_entry.focus_set()
        root.attributes("-topmost", True)
        root.after(300, lambda: root.attributes("-topmost", False))

    # -- 小工具 -------------------------------------------------------
    def _center(self, width: int, height: int) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _entry(self, parent, variable: tk.StringVar, show: str = "") -> tk.Entry:
        entry = tk.Entry(
            parent, textvariable=variable,
            show=show, width=40, bg="#ffffff", fg=TEXT,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            insertbackground=TEXT, font=(FONT, 10),
        )
        entry.pack(anchor="w", padx=24, ipady=6)
        return entry

    def _toggle_key(self) -> None:
        showing = not self.show_var.get()
        self.show_var.set(showing)
        self.key_entry.configure(show="" if showing else "*")
        self.show_btn.configure(text="Hide key" if showing else "Show key")

    def _say(self, text: str, color: str = MUTED) -> None:
        self.status.configure(text=text, fg=color)

    # -- 校验 ---------------------------------------------------------
    def _check_email_quietly(self, _event=None) -> None:
        """失焦时先做一次纯格式检查，错误早点暴露。"""
        raw = self.email_var.get().strip()
        if not raw:
            self._say("")
            return
        _, err = settings.validate_email(raw)
        if err:
            self._say(f"Email: {err}", ERROR)
        elif not settings.is_gmail(raw):
            self._say("Note: this does not look like a Gmail address", MUTED)
        else:
            self._say("Email format looks valid", SUCCESS)

    def _resolve(self) -> tuple[str, str] | None:
        """取用户输入，空则回落默认值，并做完整校验。"""
        email = self.email_var.get().strip() or self.defaults["email"]
        normalized, err = settings.validate_email(email)
        if err:
            self._say(f"Email: {err}", ERROR)
            self.email_entry.focus_set()
            return None

        raw_key = self.key_var.get().strip() or self.defaults["key"]
        if not raw_key:
            self._say("API key: no default configured — please type one", ERROR)
            self.key_entry.focus_set()
            return None
        api_key, err = settings.validate_api_key(raw_key)
        if err:
            self._say(f"API key: {err}", ERROR)
            self.key_entry.focus_set()
            return None

        if not settings.is_gmail(normalized):
            if not messagebox.askyesno(
                "Confirm", f"{normalized} is not a Gmail address.\nSave anyway?", parent=self.root
            ):
                return None

        return normalized, api_key

    # -- 动作 ---------------------------------------------------------
    def _submit(self) -> None:
        if self._busy:
            return
        resolved = self._resolve()
        if not resolved:
            return
        email, api_key = resolved

        self._busy = True
        self.save_btn.configure(state="disabled")
        self._say("Checking Gmail authorization…", MUTED)
        self.root.update_idletasks()

        check = settings.check_gmail_account(email)
        state = check["state"]

        if state == "mismatch":
            self._busy = False
            self.save_btn.configure(state="normal")
            if not messagebox.askyesno(
                "Accounts differ",
                f"You entered: {email}\n{check['message']}.\n\nSave anyway?",
                parent=self.root,
            ):
                self._say("Save cancelled", MUTED)
                return
        elif state == "not_authorized":
            self._say(
                "⚠ Not OAuth-authorized yet: no usable token on this machine "
                "(auth.py was never run, or the refresh token expired). "
                "Only the format was checked — run auth.py after saving.",
                WARN,
            )
            self.root.update_idletasks()
        elif state == "error":
            self._say(f"⚠ Could not verify the account ({check['message']}) — only the format was checked", WARN)
            self.root.update_idletasks()
        else:
            self._say(f"✓ {check['message']}", SUCCESS)
            self.root.update_idletasks()

        try:
            path = settings.save(email, api_key)
        except OSError as exc:
            self._busy = False
            self.save_btn.configure(state="normal")
            self._say(f"Could not write .env: {exc}", ERROR)
            return

        self.result = {"email": email, "key_tail": api_key[-4:], "env": str(path)}
        self._say(f"Saved to {path.name} — closing…", SUCCESS)
        print(f"[setup] saved GMAIL_ADDRESS={email}, DEEPSEEK_API_KEY=****{api_key[-4:]}")
        self.root.update_idletasks()
        self.root.after(600, self.root.destroy)

    def _cancel(self) -> None:
        self.result = None
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"Could not create a window ({exc}). Run this from a desktop session.")
        return 1

    app = SetupApp(root)
    root.mainloop()
    return 0 if app.result else 1


if __name__ == "__main__":
    raise SystemExit(main())
