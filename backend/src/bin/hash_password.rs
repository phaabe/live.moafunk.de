use argon2::{
    password_hash::{PasswordHasher, SaltString},
    Argon2,
};
use rand::rngs::OsRng;

fn main() {
    let args: Vec<String> = std::env::args().collect();

    if args.len() != 2 {
        eprintln!("Usage: hash_password <password>");
        std::process::exit(1);
    }

    let password = &args[1];
    let salt = SaltString::generate(&mut OsRng);
    let argon2 = Argon2::default();

    match argon2.hash_password(password.as_bytes(), &salt) {
        Ok(hash) => {
            println!("Password hash:");
            println!("{}", hash.to_string());
            println!();
            println!("Add this to your .env file (single quotes are required —");
            println!("the hash contains '$' which dotenvy would otherwise expand):");
            println!("SUPERADMIN_PASSWORD_HASH='{}'", hash.to_string());
        }
        Err(e) => {
            eprintln!("Error hashing password: {}", e);
            std::process::exit(1);
        }
    }
}
