//! Где взять `llama-server`, чтобы пользователю НИЧЕГО не приходилось ставить и указывать.
//! Порядок (первое найденное): (1) `YANDI_LLAMA_SERVER` — явное переопределение (для разработчика); (2) ВШИТЫЙ в бинарник узла
//! (распаковывается при первом запуске в каталог данных узла, повторно не пишется); (3) файл `llama-server` рядом с бинарником узла;
//! (4) каталог данных узла `…/yandi/bin/`; (5) PATH.
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

/// То, что вшито при сборке (`build.rs`); пусто, если не задано `YANDI_EMBED_LLAMA_SERVER`.
static EMBEDDED: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/llama_server.bin"));

pub const FILE_NAME: &str = if cfg!(windows) { "llama-server.exe" } else { "llama-server" };

/// Каталог данных узла (тот же корень, что у хранилища настроек).
pub fn data_dir() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(".")).join(".local/share/yandi")
}

/// Есть ли вшитый движок в этой сборке.
pub fn has_embedded() -> bool {
    !EMBEDDED.is_empty()
}

/// Распаковать `payload` в `dir` под именем с отпечатком содержимого; существующий файл того же размера не трогается.
/// Запись через временный файл + переименование (недописанный файл никогда не станет «готовым»), права 0755.
pub fn extract(payload: &[u8], dir: &Path) -> Result<PathBuf, String> {
    let hash = Sha256::digest(payload);
    let tag: String = hash.iter().take(6).map(|b| format!("{b:02x}")).collect();
    let target = dir.join(format!("llama-server-{tag}{}", if cfg!(windows) { ".exe" } else { "" }));
    if fs::metadata(&target).map(|m| m.len() == payload.len() as u64).unwrap_or(false) {
        return Ok(target);
    }
    fs::create_dir_all(dir).map_err(|e| format!("не удалось создать {}: {e}", dir.display()))?;
    let tmp = dir.join(format!(".llama-server-{tag}.tmp{}", std::process::id()));
    let write = || -> std::io::Result<()> {
        let mut f = fs::File::create(&tmp)?;
        f.write_all(payload)?;
        f.sync_all()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&tmp, fs::Permissions::from_mode(0o755))?;
        }
        fs::rename(&tmp, &target)
    };
    write().map_err(|e| {
        let _ = fs::remove_file(&tmp);
        format!("не удалось распаковать встроенный движок в {}: {e}", target.display())
    })?;
    Ok(target)
}

/// Чистая логика выбора (все входы явные — проверяется тестами).
pub fn resolve_with(env_override: Option<&str>, embedded: &[u8], exe_dir: Option<&Path>, data: &Path) -> String {
    if let Some(p) = env_override.filter(|s| !s.is_empty()) {
        return p.to_string();
    }
    if !embedded.is_empty() {
        if let Ok(p) = extract(embedded, &data.join("bin")) {
            return p.to_string_lossy().into_owned();
        }
    }
    if let Some(d) = exe_dir {
        let p = d.join(FILE_NAME);
        if p.is_file() {
            return p.to_string_lossy().into_owned();
        }
    }
    let p = data.join("bin").join(FILE_NAME);
    if p.is_file() {
        return p.to_string_lossy().into_owned();
    }
    FILE_NAME.to_string() // поиск в PATH
}

/// Выбор для работающего узла.
pub fn resolve() -> String {
    let exe_dir = std::env::current_exe().ok().and_then(|p| p.parent().map(|d| d.to_path_buf()));
    resolve_with(std::env::var("YANDI_LLAMA_SERVER").ok().as_deref(), EMBEDDED, exe_dir.as_deref(), &data_dir())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("yandi_eb_{}_{}", tag, std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn env_override_wins_over_everything() {
        let d = tmp("env");
        assert_eq!(resolve_with(Some("/x/y"), b"payload", Some(&d), &d), "/x/y");
        // пустая переменная = не задана
        assert_ne!(resolve_with(Some(""), b"", None, &d), "");
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn embedded_is_extracted_once_executable_and_preferred_over_neighbours() {
        let d = tmp("emb");
        let exe = d.join("exe");
        fs::create_dir_all(&exe).unwrap();
        fs::write(exe.join(FILE_NAME), b"neighbour").unwrap();
        let p = PathBuf::from(resolve_with(None, b"PAYLOAD", Some(&exe), &d));
        assert!(p.starts_with(d.join("bin")), "{p:?}");
        assert_eq!(fs::read(&p).unwrap(), b"PAYLOAD");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(fs::metadata(&p).unwrap().permissions().mode() & 0o777, 0o755);
        }
        // повторно не пишется (метка времени не меняется)
        let m1 = fs::metadata(&p).unwrap().modified().unwrap();
        std::thread::sleep(std::time::Duration::from_millis(30));
        assert_eq!(PathBuf::from(resolve_with(None, b"PAYLOAD", Some(&exe), &d)), p);
        assert_eq!(fs::metadata(&p).unwrap().modified().unwrap(), m1);
        // повреждённый (другой размер) — пересоздаётся
        fs::write(&p, b"broken").unwrap();
        assert_eq!(PathBuf::from(resolve_with(None, b"PAYLOAD", Some(&exe), &d)), p);
        assert_eq!(fs::read(&p).unwrap(), b"PAYLOAD");
        // другой движок → другое имя, старый не мешает
        let p2 = PathBuf::from(resolve_with(None, b"PAYLOAD-2", Some(&exe), &d));
        assert_ne!(p, p2);
        assert_eq!(fs::read(&p2).unwrap(), b"PAYLOAD-2");
        // временных файлов не осталось
        assert!(fs::read_dir(d.join("bin")).unwrap().all(|e| !e.unwrap().file_name().to_string_lossy().starts_with('.')));
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn without_embedded_order_is_neighbour_then_data_dir_then_path() {
        let d = tmp("order");
        let exe = d.join("exe");
        fs::create_dir_all(&exe).unwrap();
        assert_eq!(resolve_with(None, b"", Some(&exe), &d), FILE_NAME);
        fs::create_dir_all(d.join("bin")).unwrap();
        fs::write(d.join("bin").join(FILE_NAME), b"data").unwrap();
        assert_eq!(PathBuf::from(resolve_with(None, b"", Some(&exe), &d)), d.join("bin").join(FILE_NAME));
        fs::write(exe.join(FILE_NAME), b"near").unwrap();
        assert_eq!(PathBuf::from(resolve_with(None, b"", Some(&exe), &d)), exe.join(FILE_NAME));
        // каталог вместо файла не считается
        let d2 = tmp("order2");
        fs::create_dir_all(d2.join("bin").join(FILE_NAME)).unwrap();
        assert_eq!(resolve_with(None, b"", None, &d2), FILE_NAME);
        // и рядом с узлом каталог с таким именем — не движок
        let exe2 = d2.join("exe");
        fs::create_dir_all(exe2.join(FILE_NAME)).unwrap();
        assert_eq!(resolve_with(None, b"", Some(&exe2), &d2), FILE_NAME);
        let _ = fs::remove_dir_all(&d);
        let _ = fs::remove_dir_all(&d2);
    }

    #[test]
    fn failed_rename_leaves_no_temp_file() {
        let d = tmp("tmpclean");
        let bin = d.join("bin");
        let hash = Sha256::digest(b"PAYLOAD");
        let tag: String = hash.iter().take(6).map(|b| format!("{b:02x}")).collect();
        // на месте будущего файла — непустой каталог: переименование невозможно
        let blocker = bin.join(format!("llama-server-{tag}"));
        fs::create_dir_all(blocker.join("inner")).unwrap();
        assert!(extract(b"PAYLOAD", &bin).is_err());
        assert!(fs::read_dir(&bin).unwrap().all(|e| !e.unwrap().file_name().to_string_lossy().starts_with('.')));
        let _ = fs::remove_dir_all(&d);
    }

    #[test]
    fn extraction_failure_falls_through_instead_of_panicking() {
        let d = tmp("fail");
        // «каталог данных» — обычный файл: создать bin/ нельзя
        let data = d.join("file");
        fs::write(&data, b"x").unwrap();
        assert_eq!(resolve_with(None, b"PAYLOAD", None, &data), FILE_NAME);
        assert!(extract(b"PAYLOAD", &data.join("bin")).unwrap_err().contains("не удалось"));
        let _ = fs::remove_dir_all(&d);
    }
}
