//! yandi-keys — look at, migrate and recover the node's key directory.
//!
//!   yandi-keys status  [--dir D] [--port N]
//!   yandi-keys migrate [--dir D] [--port N] [--password-stdin] [--machine-id ID]   legacy → recoverable root (run where everything still opens)
//!   yandi-keys recover [--dir D] [--port N] [--password-stdin] [--machine-id ID]   new machine / lost device key: the recovery password
//!
//! It never prints a password or a key. `--password-stdin` reads the password (and, for `migrate`, its confirmation) as lines from
//! standard input; without it the password is asked on the terminal with echo off.
use key_root::{
    status, DeviceKeyProvider, FileDeviceKey, FixedMachine, KdfParams, KdfPolicy, KeyDir,
    KeyRootError, MachineContext, Migration, Recovery, SystemMachine,
};
use std::io::{BufRead, Write};
use std::path::PathBuf;
use zeroize::Zeroizing;

struct Args {
    command: String,
    dir: PathBuf,
    port: u16,
    password_stdin: bool,
    machine_id: Option<String>,
}

fn parse_args() -> Result<Args, String> {
    let mut it = std::env::args().skip(1);
    let command = it.next().ok_or("usage: yandi-keys status|migrate|recover [--dir D] [--port N] [--password-stdin] [--machine-id ID]")?;
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or("HOME is not set")?;
    let mut args = Args {
        command,
        dir: home.join(".yandi_keys"),
        port: 9000,
        password_stdin: false,
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
            "--password-stdin" => args.password_stdin = true,
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
        .map_err(|_| "could not read the password")?;
    while line.ends_with('\n') || line.ends_with('\r') {
        line.pop();
    }
    Ok(line)
}

fn read_password(prompt: &str, from_stdin: bool) -> Result<Zeroizing<String>, String> {
    if from_stdin {
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
        eprint!("{prompt}");
        let _ = std::io::stderr().flush();
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
            let pw = read_password(
                "New recovery password (at least 12 characters): ",
                args.password_stdin,
            )?;
            let again = read_password("Repeat it: ", args.password_stdin)?;
            if *pw != *again {
                eprintln!("yandi-keys: the two passwords differ; nothing was changed");
                return Ok(2);
            }
            let env_pw = std::env::var("YANDI_KEY_PASSWORD").ok();
            let policy = KdfPolicy::production();
            let m = Migration {
                dir: &dir,
                port: args.port,
                machine: machine.as_ref(),
                env_password: env_pw.as_deref(),
                device: &device,
                recovery_password: &pw,
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
        "recover" => {
            let pw = read_password("Recovery password: ", args.password_stdin)?;
            let policy = KdfPolicy::production();
            let r = Recovery {
                dir: &dir,
                port: args.port,
                machine: machine.as_ref(),
                device: &device,
                password: &pw,
                policy: &policy,
            };
            match r.run() {
                Ok(r) => {
                    println!("recovered. node id {} is the same as before.", r.node_id);
                    println!("a new device key was made for this machine ({}); the next start needs no password.", r.device_protection.describe());
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
