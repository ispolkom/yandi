//! yandi-keys — look at, migrate and recover the node's key directory.
//!
//!   yandi-keys status
//!   yandi-keys migrate           legacy → recoverable root; makes and shows a RECOVERY CODE (write it down) — run where everything still opens
//!   yandi-keys new-recovery-code replace the recovery secret by a fresh recovery code (the device opens the key; identity and chats untouched)
//!   yandi-keys recover           new machine / lost device key: type the recovery code
//!
//!   options: --dir D (default ~/.yandi_keys)  --port N (9000)  --machine-id ID
//!            --own-password   (migrate: use a password you choose instead of a generated code; typed with no echo)
//!            --hidden         (recover: do not show what you type)
//!            --password-stdin (read every answer as a line from standard input; for scripts and tests)
//!
//! It never prints a key. The recovery code is printed once, on purpose, by `migrate` and `new-recovery-code`; it is never stored.
use key_root::{
    recovery_code::looks_like_code, status, DeviceKeyProvider, FileDeviceKey, FixedMachine,
    KdfParams, KdfPolicy, KeyDir, KeyRootError, MachineContext, Migration, NewRecoveryCode,
    Recovery, RecoveryCode, SystemMachine,
};
use std::io::{BufRead, Write};
use std::path::PathBuf;
use zeroize::Zeroizing;

struct Args {
    command: String,
    dir: PathBuf,
    port: u16,
    stdin: bool,
    own_password: bool,
    hidden: bool,
    machine_id: Option<String>,
}

fn parse_args() -> Result<Args, String> {
    let mut it = std::env::args().skip(1);
    let command = it
        .next()
        .ok_or("usage: yandi-keys status|migrate|new-recovery-code|recover [options]")?;
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or("HOME is not set")?;
    let mut args = Args {
        command,
        dir: home.join(".yandi_keys"),
        port: 9000,
        stdin: false,
        own_password: false,
        hidden: false,
        machine_id: None,
    };
    while let Some(flag) = it.next() {
        match flag.as_str() {
            "--dir" => args.dir = PathBuf::from(it.next().ok_or("--dir needs a value")?),
            "--port" => {
                args.port = it
                    .next()
                    .ok_or("--port needs a value")?
                    .parse()
                    .map_err(|_| "--port must be a number")?
            }
            "--machine-id" => {
                args.machine_id = Some(it.next().ok_or("--machine-id needs a value")?)
            }
            "--password-stdin" | "--stdin" => args.stdin = true,
            "--own-password" => args.own_password = true,
            "--hidden" => args.hidden = true,
            other => return Err(format!("unknown option {other}")),
        }
    }
    Ok(args)
}

fn read_line_stdin() -> Result<Zeroizing<String>, String> {
    let mut line = Zeroizing::new(String::new());
    std::io::stdin()
        .lock()
        .read_line(&mut line)
        .map_err(|_| "could not read the answer")?;
    while line.ends_with('\n') || line.ends_with('\r') {
        line.pop();
    }
    Ok(line)
}

/// Ask a question. `hidden`: the answer is not echoed (a terminal is required); otherwise it is shown as typed.
fn ask(prompt: &str, from_stdin: bool, hidden: bool) -> Result<Zeroizing<String>, String> {
    if from_stdin {
        return read_line_stdin();
    }
    eprint!("{prompt}");
    let _ = std::io::stderr().flush();
    if !hidden {
        return read_line_stdin();
    }
    unsafe {
        if libc::isatty(0) != 1 {
            return Err("standard input is not a terminal; use --password-stdin".into());
        }
        let mut old: libc::termios = std::mem::zeroed();
        libc::tcgetattr(0, &mut old);
        let mut quiet = old;
        quiet.c_lflag &= !libc::ECHO;
        libc::tcsetattr(0, libc::TCSANOW, &quiet);
        let line = read_line_stdin();
        libc::tcsetattr(0, libc::TCSANOW, &old);
        eprintln!();
        line
    }
}

fn fail(e: &KeyRootError) -> i32 {
    eprintln!("yandi-keys: {} — {}", e.category(), e);
    2
}

fn show_code(code: &RecoveryCode) {
    println!();
    println!("    ┌───────────────────────────────────────────────────────┐");
    println!("    │  RECOVERY CODE:  {}  │", code.display());
    println!("    └───────────────────────────────────────────────────────┘");
    println!();
    println!("Write it down on paper NOW, exactly as shown. It is the only way to get your identity back on a new machine, and it is");
    println!("shown once: it is not stored anywhere and cannot be recovered. Do not photograph it, do not keep it next to the key files.");
    println!();
}

/// Show the code, then require it to be typed back (visible) before anything is written: proof that it was copied correctly.
fn confirmed_code(args: &Args) -> Result<Option<RecoveryCode>, String> {
    let code = RecoveryCode::generate();
    show_code(&code);
    for attempt in 1..=3 {
        let typed = ask(
            "Type the code back to confirm you have written it down: ",
            args.stdin,
            false,
        )?;
        match RecoveryCode::parse(&typed) {
            Ok(t) if t.same_as(&code) => return Ok(Some(code)),
            Ok(_) => eprintln!(
                "That is a valid code, but not the one shown. Check what you wrote ({attempt}/3)."
            ),
            Err(e) => eprintln!("{e} ({attempt}/3)."),
        }
    }
    eprintln!("yandi-keys: the code was not confirmed; nothing was changed");
    Ok(None)
}

fn run() -> Result<i32, String> {
    let args = parse_args()?;
    let machine: Box<dyn MachineContext> = match &args.machine_id {
        Some(id) => Box::new(FixedMachine(id.clone())),
        None => Box::new(SystemMachine),
    };
    let dir = match KeyDir::open(&args.dir) {
        Ok(d) => d,
        Err(e) => return Ok(fail(&e)),
    };
    let device = FileDeviceKey::new(dir.file("device.key"));
    let policy = KdfPolicy::production();
    match args.command.as_str() {
        "status" => {
            let s = status(&dir, args.port);
            println!("key directory : {}", dir.path().display());
            println!(
                "auth.json     : {}",
                match &s.auth {
                    Ok(Some(f)) => format!("{f:?}"),
                    Ok(None) => "absent".into(),
                    Err(e) => format!("unreadable ({})", e.category()),
                }
            );
            println!(
                "identity ({}) : {}",
                args.port,
                match &s.identity {
                    Ok(Some(f)) => format!("{f:?}"),
                    Ok(None) => "absent".into(),
                    Err(e) => format!("unreadable ({})", e.category()),
                }
            );
            if !s.other_identity_files.is_empty() {
                println!(
                    "other identity files: {}",
                    s.other_identity_files.join(", ")
                );
            }
            println!(
                "device key    : {}",
                match device.load() {
                    Ok(Some(_)) => "present".to_owned(),
                    Ok(None) => "absent".to_owned(),
                    Err(e) => format!("unreadable ({})", e.category()),
                }
            );
            println!(
                "device key protection when created here: {}",
                device.protection().describe()
            );
            Ok(0)
        }
        "migrate" => {
            let env_pw = std::env::var("YANDI_KEY_PASSWORD").ok();
            let secret: Zeroizing<String> = if args.own_password {
                let pw = ask(
                    "New recovery password (at least 12 characters): ",
                    args.stdin,
                    true,
                )?;
                let again = ask("Repeat it: ", args.stdin, true)?;
                if *pw != *again {
                    eprintln!("yandi-keys: the two passwords differ; nothing was changed");
                    return Ok(2);
                }
                pw
            } else {
                match confirmed_code(&args)? {
                    Some(code) => Zeroizing::new(code.secret().to_owned()),
                    None => return Ok(2),
                }
            };
            let m = Migration {
                dir: &dir,
                port: args.port,
                machine: machine.as_ref(),
                env_password: env_pw.as_deref(),
                device: &device,
                recovery_password: &secret,
                params: KdfParams::RECOMMENDED,
                policy: &policy,
            };
            match m.run() {
                Ok(r) => {
                    println!("migrated. node id {} is unchanged.", r.node_id);
                    println!(
                        "recovery wrapper written (Argon2id, {} MiB); device key {}.",
                        KdfParams::RECOMMENDED.memory_kib / 1024,
                        if r.device_key_created {
                            "created"
                        } else {
                            "reused"
                        }
                    );
                    println!("device key protection: {}", r.device_protection.describe());
                    for b in &r.backups {
                        println!("backup kept: {}", b.display());
                    }
                    println!("The backups are the OLD files, protected only by the public machine id. Keep them until you have restarted the node and checked it,");
                    println!("then move them somewhere safe or delete them yourself; this tool never deletes them.");
                    Ok(0)
                }
                Err(e) => Ok(fail(&e)),
            }
        }
        "new-recovery-code" => {
            let op = NewRecoveryCode {
                dir: &dir,
                machine: machine.as_ref(),
                device: &device,
                params: KdfParams::RECOMMENDED,
                policy: &policy,
            };
            let pending = match op.prepare() {
                Ok(p) => p,
                Err(e) => return Ok(fail(&e)),
            };
            show_code(pending.code());
            for attempt in 1..=3 {
                let typed = ask(
                    "Type the code back to confirm you have written it down: ",
                    args.stdin,
                    false,
                )?;
                match op.commit(&pending, &typed) {
                    Ok(backup) => {
                        println!("the recovery code was replaced. The previous recovery password (or code) NO LONGER WORKS.");
                        println!("the node id, the key and the chats are unchanged. previous auth.json kept as {}", backup.display());
                        return Ok(0);
                    }
                    Err(KeyRootError::Invalid(why)) => eprintln!("{why} ({attempt}/3)."),
                    Err(e) => return Ok(fail(&e)),
                }
            }
            eprintln!("yandi-keys: the code was not confirmed; nothing was changed");
            Ok(2)
        }
        "recover" => {
            let typed = ask("Recovery code: ", args.stdin, args.hidden)?;
            if looks_like_code(&typed) {
                if let Err(e) = RecoveryCode::parse(&typed) {
                    eprintln!("yandi-keys: {e}. Nothing was changed.");
                    return Ok(2);
                }
            }
            let secret = key_root::recovery_secret(&typed);
            let r = Recovery {
                dir: &dir,
                port: args.port,
                machine: machine.as_ref(),
                device: &device,
                password: &secret,
                policy: &policy,
            };
            match r.run() {
                Ok(r) => {
                    println!("recovered. node id {} is the same as before.", r.node_id);
                    println!("a new device key was made for this machine ({}); the next start needs no code.", r.device_protection.describe());
                    for b in &r.backups {
                        println!("backup kept: {}", b.display());
                    }
                    Ok(0)
                }
                Err(e) => Ok(fail(&e)),
            }
        }
        other => Err(format!("unknown command {other}")),
    }
}

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(msg) => {
            eprintln!("yandi-keys: {msg}");
            std::process::exit(2);
        }
    }
}
