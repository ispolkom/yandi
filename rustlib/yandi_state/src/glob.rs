//! Шаблоны ключей — побайтовый порт `stringmatchlen` из Redis (`*`, `?`, `[abc]`, `[^a]`, `[a-z]`, экранирование `\`).

pub fn glob_match(pattern: &[u8], string: &[u8]) -> bool {
    matchlen(pattern, string)
}

fn matchlen(mut p: &[u8], mut s: &[u8]) -> bool {
    while !p.is_empty() && !s.is_empty() {
        match p[0] {
            b'*' => {
                while p.len() > 1 && p[1] == b'*' {
                    p = &p[1..];
                }
                if p.len() == 1 {
                    return true;
                }
                while !s.is_empty() {
                    if matchlen(&p[1..], s) {
                        return true;
                    }
                    s = &s[1..];
                }
                return false;
            }
            b'?' => {
                s = &s[1..];
            }
            b'[' => {
                p = &p[1..];
                let not = !p.is_empty() && p[0] == b'^';
                if not {
                    p = &p[1..];
                }
                let mut matched = false;
                loop {
                    if p.len() >= 2 && p[0] == b'\\' {
                        p = &p[1..];
                        if p[0] == s[0] {
                            matched = true;
                        }
                    } else if !p.is_empty() && p[0] == b']' {
                        break;
                    } else if p.is_empty() {
                        // шаблон кончился внутри [...]: как в C — шаг назад и выход
                        break;
                    } else if p.len() >= 3 && p[1] == b'-' {
                        let (mut start, mut end) = (p[0], p[2]);
                        let c = s[0];
                        if start > end {
                            std::mem::swap(&mut start, &mut end);
                        }
                        p = &p[2..];
                        if c >= start && c <= end {
                            matched = true;
                        }
                    } else if p[0] == s[0] {
                        matched = true;
                    }
                    p = &p[1..];
                }
                if not {
                    matched = !matched;
                }
                if !matched {
                    return false;
                }
                s = &s[1..];
                // p указывает на ']' (или на конец) — общий шаг ниже пропустит его
                if p.is_empty() {
                    // как в C: pattern-- / patternLen++ → после общего шага patternLen == 0
                    return s.is_empty();
                }
            }
            b'\\' => {
                if p.len() >= 2 {
                    p = &p[1..];
                }
                if p[0] != s[0] {
                    return false;
                }
                s = &s[1..];
            }
            c => {
                if c != s[0] {
                    return false;
                }
                s = &s[1..];
            }
        }
        p = &p[1..];
        if s.is_empty() {
            while !p.is_empty() && p[0] == b'*' {
                p = &p[1..];
            }
            break;
        }
    }
    p.is_empty() && s.is_empty()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn basics() {
        assert!(glob_match(b"*", b"anything"));
        assert!(glob_match(b"h?llo", b"hello"));
        assert!(glob_match(b"h[ae]llo", b"hallo"));
        assert!(!glob_match(b"h[^e]llo", b"hello"));
        assert!(glob_match(b"h[a-b]llo", b"hbllo"));
        assert!(glob_match(b"council:*", b"council:chat:turn"));
        assert!(!glob_match(b"council:?", b"council:ab"));
        assert!(glob_match(b"a\\*b", b"a*b"));
        assert!(glob_match(b"", b""));
        assert!(!glob_match(b"", b"x"));
        assert!(glob_match(b"a*", b"a"));
        assert!(glob_match(b"*a*b*", b"xxaxxbxx"));
    }
}
