//! АВТОГЕНЕРИРОВАНО rustlib/gen_policy_data.py из agent/policy.py — не править руками.

/// (тип находки, исходный текст паттерна Python `re`) в порядке сканирования
pub static SECRET_PATTERNS: &[(&str, &str)] = &[
    (r##"api_key"##, r##"(?i)(api[_-]?key|apikey)\s*[=:]\s*["\']?([A-Za-z0-9_\-]{20,})["\']?"##),
    (r##"sk_token"##, r##"\bsk-[A-Za-z0-9]{20,}\b"##),
    (r##"aws_access"##, r##"\bAKIA[A-Z0-9]{16}\b"##),
    (r##"aws_secret"##, r##"(?i)aws[_\-]?secret[_\-]?access[_\-]?key\s*[=:]\s*["\']?([A-Za-z0-9+/]{40})["\']?"##),
    (r##"password"##, r##"(?i)(password|passwd|pwd)\s*[=:]\s*["\']([^"\'\s]{6,})["\']"##),
    (r##"private_key"##, r##"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"##),
    (r##"token"##, r##"(?i)(token|secret)\s*[=:]\s*["\']([A-Za-z0-9_\-\.]{16,})["\']"##),
    (r##"hf_token"##, r##"\bhf_[A-Za-z0-9]{30,}\b"##),
    (r##"proxy_creds"##, r##"https?://[^:]+:[^@]+@[a-zA-Z0-9._-]+"##),
];
pub static SECRET_WHITELIST: &[&str] = &[r##"<TOKEN>"##, r##"YOUR_KEY"##, r##"api_key_here"##, r##"changeme"##, r##"example"##, r##"password123"##, r##"sk-xxxx"##, r##"your_token_here"##];
pub static SAFE_HOSTS: &[&str] = &[r##"127.0.0.1"##, r##"api.anthropic.com"##, r##"api.openai.com"##, r##"chat.deepseek.com"##, r##"chatgpt.com"##, r##"claude.ai"##, r##"huggingface.co"##, r##"localhost"##, r##"user:pass@host"##];
pub static SHELL_ALLOWLIST: &[&str] = &[r##"ls"##, r##"cat"##, r##"head"##, r##"tail"##, r##"wc"##, r##"find"##, r##"grep"##, r##"rg"##, r##"python3"##, r##"python"##, r##"git status"##, r##"git log"##, r##"git diff"##, r##"git show"##, r##"redis-cli"##, r##"./ctl"##, r##"du "##, r##"df "##, r##"free "##, r##"uptime"##, r##"ps aux"##, r##"journalctl"##, r##"systemctl status"##, r##"sqlite3"##];
pub static SHELL_BLOCKLIST: &[&str] = &[r##"rm -rf"##, r##"rm -fr"##, r##"sudo rm"##, r##"dd if="##, r##"mkfs"##, r##"> /dev/"##, r##"chmod 777"##, r##"curl | bash"##, r##"wget | bash"##, r##"curl -o- | bash"##, r##"eval $("##, r##"`"##, r##"nc -"##, r##"netcat"##, r##"; rm "##, r##"&& rm "##, r##"passwd "##, r##"adduser "##, r##"userdel "##, r##"iptables -F"##, r##"ufw disable"##];
pub static NETWORK_ALLOW: &[&str] = &[r##"127.0.0.1"##, r##"localhost"##, r##"api.anthropic.com"##, r##"api.openai.com"##, r##"api.deepseek.com"##, r##"huggingface.co"##, r##"cdn-lfs.huggingface.co"##, r##"pypi.org"##, r##"files.pythonhosted.org"##];
