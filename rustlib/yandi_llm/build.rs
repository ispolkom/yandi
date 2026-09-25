//! Вшивание `llama-server` в бинарник узла. Если при сборке задана `YANDI_EMBED_LLAMA_SERVER=/путь/к/llama-server`, файл копируется в
//! артефакты сборки и `include_bytes!` кладёт его внутрь бинарника; узел при первом запуске сам распаковывает его (см. `engine_binary.rs`).
//! Без переменной вшивается пустой файл — узел ищет `llama-server` рядом/в каталоге данных/в PATH.
use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-env-changed=YANDI_EMBED_LLAMA_SERVER");
    let out = PathBuf::from(env::var("OUT_DIR").unwrap()).join("llama_server.bin");
    match env::var("YANDI_EMBED_LLAMA_SERVER") {
        Ok(p) if !p.is_empty() => {
            println!("cargo:rerun-if-changed={p}");
            fs::copy(&p, &out).unwrap_or_else(|e| panic!("YANDI_EMBED_LLAMA_SERVER={p}: не удалось прочитать файл: {e}"));
        }
        _ => fs::write(&out, b"").unwrap(),
    }
}
