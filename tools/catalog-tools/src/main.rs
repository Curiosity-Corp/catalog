//! `catalog-tools`: the catalog's tool binary. See tools/README.md.

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use anyhow::{Context, Result};
use catalog_tools::packages::{self, Evidence, PlatformResult};
use catalog_tools::{find_root, reference};
use clap::{Args, Parser, Subcommand};

#[derive(Parser)]
#[command(
    name = "catalog-tools",
    version,
    about = "Tools for the curiosity.workstation collection"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Generate docs/reference.md, the supported variable and tag reference.
    Reference(ReferenceArgs),
    /// Which of the role's apt packages each platform's archive provides.
    #[command(subcommand)]
    Packages(PackagesCommand),
}

#[derive(Args)]
struct RootArg {
    /// Catalog checkout (default: found from the current directory).
    #[arg(long, global = true)]
    root: Option<PathBuf>,
}

#[derive(Args)]
struct ReferenceArgs {
    #[command(flatten)]
    root: RootArg,
    /// Reference file to write or check (default: <root>/docs/reference.md).
    #[arg(long)]
    output: Option<PathBuf>,
    /// Exit 1 when the file is out of date instead of writing it.
    #[arg(long)]
    check: bool,
}

#[derive(Subcommand)]
enum PackagesCommand {
    /// Print the role's package names, one per line.
    List {
        #[command(flatten)]
        root: RootArg,
    },
    /// Ask this system's apt about every package and write one platform result.
    Probe {
        #[command(flatten)]
        root: RootArg,
        /// Name of the platform in the result, for example the image `ubuntu:24.04`.
        #[arg(long)]
        image: String,
        /// Where to write the result (JSON).
        #[arg(long)]
        output: PathBuf,
        /// Private directory for apt's lists and caches.
        #[arg(long, default_value = ".apt-probe")]
        apt_dir: PathBuf,
        /// Use lists already in --apt-dir instead of running apt-get update.
        #[arg(long)]
        no_update: bool,
    },
    /// Combine platform results into a dated evidence file.
    Assemble {
        #[command(flatten)]
        root: RootArg,
        /// Evidence file to write.
        #[arg(long)]
        output: PathBuf,
        /// Date of the probe, YYYY-MM-DD (default: today, UTC).
        #[arg(long)]
        date: Option<String>,
        /// Platform result files from `probe`.
        #[arg(required = true)]
        results: Vec<PathBuf>,
    },
    /// Exit 1 when an evidence file does not match the role's package list.
    Check {
        #[command(flatten)]
        root: RootArg,
        /// Evidence file (default: the newest docs/evidence/platform-packages-*.json).
        file: Option<PathBuf>,
    },
}

/// Exit codes: 0 done or up to date, 1 out of date or problems found, 2 error.
fn main() -> ExitCode {
    let cli = Cli::parse();
    let outcome = match cli.command {
        Command::Reference(args) => run_reference(&args),
        Command::Packages(command) => run_packages(command),
    };
    match outcome {
        Ok(code) => code,
        Err(error) => {
            eprintln!("catalog-tools: {error:#}");
            ExitCode::from(2)
        }
    }
}

fn resolve_root(arg: &RootArg) -> Result<PathBuf> {
    match &arg.root {
        Some(root) => Ok(root.clone()),
        None => find_root(&std::env::current_dir().context("cannot read the current directory")?),
    }
}

fn run_reference(args: &ReferenceArgs) -> Result<ExitCode> {
    let root = resolve_root(&args.root)?;
    let output = args
        .output
        .clone()
        .unwrap_or_else(|| root.join(reference::OUTPUT));
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
    write_file(&output, &text)?;
    println!("wrote {}", output.display());
    Ok(ExitCode::SUCCESS)
}

fn run_packages(command: PackagesCommand) -> Result<ExitCode> {
    match command {
        PackagesCommand::List { root } => {
            for name in packages::role_packages(&resolve_root(&root)?)? {
                println!("{name}");
            }
            Ok(ExitCode::SUCCESS)
        }
        PackagesCommand::Probe {
            root,
            image,
            output,
            apt_dir,
            no_update,
        } => {
            let names = packages::role_packages(&resolve_root(&root)?)?;
            let result = packages::probe(&image, &names, &apt_dir, !no_update)?;
            write_file(&output, &packages::to_json(&result)?)?;
            println!(
                "{image}: {} available, {} provided by another package, {} missing: {}",
                result.available,
                result.provided_by.len(),
                result.missing.len(),
                result.missing.join(", ")
            );
            Ok(ExitCode::SUCCESS)
        }
        PackagesCommand::Assemble {
            root,
            output,
            date,
            results,
        } => {
            let names = packages::role_packages(&resolve_root(&root)?)?;
            let results = results
                .iter()
                .map(|path| packages::read_json::<PlatformResult>(path))
                .collect::<Result<Vec<_>>>()?;
            let date = date.unwrap_or_else(packages::date::today);
            let evidence = packages::assemble(&date, &names, results)?;
            write_file(&output, &packages::to_json(&evidence)?)?;
            println!(
                "wrote {} ({} platforms)",
                output.display(),
                evidence.results.len()
            );
            Ok(ExitCode::SUCCESS)
        }
        PackagesCommand::Check { root, file } => {
            let root = resolve_root(&root)?;
            let file = match file {
                Some(file) => file,
                None => packages::newest_evidence(&root)?,
            };
            let evidence: Evidence = packages::read_json(&file)?;
            let problems = packages::check(&evidence, &packages::role_packages(&root)?);
            if problems.is_empty() {
                println!(
                    "{} matches the role's {} packages ({} platforms)",
                    file.display(),
                    evidence.packages.len(),
                    evidence.results.len()
                );
                return Ok(ExitCode::SUCCESS);
            }
            for problem in &problems {
                eprintln!("{}: {problem}", file.display());
            }
            eprintln!(
                "Refresh it from the platform-evidence CI job's artifact (docs/platform-differences.md)."
            );
            Ok(ExitCode::from(1))
        }
    }
}

fn write_file(path: &Path, text: &str) -> Result<()> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("cannot create {}", parent.display()))?;
    }
    std::fs::write(path, text).with_context(|| format!("cannot write {}", path.display()))
}
