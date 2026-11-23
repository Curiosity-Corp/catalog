//! `catalog-tools`: the catalog's tool binary. See tools/README.md.

use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{Context, Result};
use catalog_tools::{find_root, reference};
use clap::{Args, Parser, Subcommand};

#[derive(Parser)]
#[command(name = "catalog-tools", version, about = "Tools for the curiosity.workstation collection")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Generate docs/reference.md, the supported variable and tag reference.
    Reference(ReferenceArgs),
}

#[derive(Args)]
struct ReferenceArgs {
    /// Catalog checkout (default: found from the current directory).
    #[arg(long)]
    root: Option<PathBuf>,
    /// Reference file to write or check (default: <root>/docs/reference.md).
    #[arg(long)]
    output: Option<PathBuf>,
    /// Exit 1 when the file is out of date instead of writing it.
    #[arg(long)]
    check: bool,
}

/// Exit codes: 0 done or up to date, 1 out of date, 2 error.
fn main() -> ExitCode {
    let cli = Cli::parse();
    let outcome = match cli.command {
        Command::Reference(args) => run_reference(&args),
    };
    match outcome {
        Ok(code) => code,
        Err(error) => {
            eprintln!("catalog-tools: {error:#}");
            ExitCode::from(2)
        }
    }
}

fn run_reference(args: &ReferenceArgs) -> Result<ExitCode> {
    let root = match &args.root {
        Some(root) => root.clone(),
        None => find_root(&std::env::current_dir().context("cannot read the current directory")?)?,
    };
    let output = args.output.clone().unwrap_or_else(|| root.join(reference::OUTPUT));
    let text = reference::generate(&root)?;
    if args.check {
        let committed = if output.is_file() {
            catalog_tools::yaml::read(&output)?
        } else {
            String::new()
        };
        return Ok(match reference::first_difference(&committed, &text) {
            None => {
                println!("{} is up to date", output.display());
                ExitCode::SUCCESS
            }
            Some(difference) => {
                eprintln!("{difference}");
                eprintln!(
                    "\n{} is out of date with the role defaults, tasks or annotations. \
                     Run `make reference` and commit the result (CI keeps the generated file as the \
                     `reference.generated.md` artifact of the `tools` job).",
                    output.display()
                );
                ExitCode::from(1)
            }
        });
    }
    std::fs::write(&output, &text).with_context(|| format!("cannot write {}", output.display()))?;
    println!("wrote {}", output.display());
    Ok(ExitCode::SUCCESS)
}
