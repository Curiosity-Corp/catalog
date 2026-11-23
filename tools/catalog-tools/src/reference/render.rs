//! Markdown rendering of the reference.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use regex::Regex;
use serde_norway::Value;

use super::Role;
use super::annotations::{Annotations, SAFETY, SECRET_NAME, STABILITY, stability_rank};
use super::tasks::PROFILE_NAMES;
use crate::yaml;

/// Longest default shown verbatim.
const MAX_DEFAULT: usize = 60;
/// Longest description kept; the defaults file has the full text.
const MAX_DESCRIPTION: usize = 300;

static PROFILE_LINE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(\S+)\s*=\s*(.+)$").expect("valid regex"));

/// Escape text for a Markdown table cell.
pub fn cell(text: &str) -> String {
    text.replace('|', "\\|")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('\n', " ")
}

/// A code span that survives a table cell.
pub fn code(text: &str) -> String {
    format!("`{}`", text.replace('|', "\\|").replace('`', "'"))
}

fn plural(count: usize, word: &str) -> String {
    format!("{count} {word}{}", if count == 1 { "" } else { "s" })
}

/// The type of a default as the reference names it.
pub fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(number) if yaml::is_integer(number) => "int",
        Value::Number(_) => "float",
        Value::String(text) if text.contains("{{") => "string (template)",
        Value::String(_) => "string",
        Value::Sequence(_) => "list",
        Value::Mapping(_) => "dict",
        Value::Tagged(tagged) => type_name(&tagged.value),
    }
}

/// A short, safe rendering of a default. Dictionaries and long values are
/// summarised, never copied, and a credential-named value is redacted.
pub fn show_default(name: &str, value: &Value) -> String {
    match value {
        Value::Mapping(mapping) => return format!("mapping, {}", plural(mapping.len(), "key")),
        Value::String(text) if text.contains("{{") && text.chars().count() > MAX_DEFAULT => {
            return "template expression".to_owned();
        }
        Value::String(text) if text.contains('\n') => return "multi-line text".to_owned(),
        _ => {}
    }
    if !yaml::is_unset(value) && SECRET_NAME.is_match(name) {
        return "(redacted)".to_owned();
    }
    let text = yaml::json(value);
    if text.chars().count() > MAX_DEFAULT {
        return match value {
            Value::Sequence(items) => format!("list, {}", plural(items.len(), "item")),
            Value::String(text) => format!("string, {} characters", text.chars().count()),
            _ => format!("value, {} characters", text.chars().count()),
        };
    }
    code(&text)
}

/// Split after `.`, `!` or `?` followed by white space.
fn sentences(text: &str) -> Vec<&str> {
    let mut parts = Vec::new();
    let mut start = 0;
    let mut chars = text.char_indices().peekable();
    while let Some((_, current)) = chars.next() {
        let Some(&(next_index, next)) = chars.peek() else {
            break;
        };
        if matches!(current, '.' | '!' | '?') && next.is_whitespace() {
            parts.push(&text[start..next_index]);
            let mut resume = text.len();
            while let Some(&(position, character)) = chars.peek() {
                if !character.is_whitespace() {
                    resume = position;
                    break;
                }
                chars.next();
            }
            start = resume;
        }
    }
    if start < text.len() {
        parts.push(&text[start..]);
    }
    parts
}

/// Keep whole sentences up to `MAX_DESCRIPTION` characters.
pub fn summarize(text: &str) -> String {
    if text.chars().count() <= MAX_DESCRIPTION {
        return text.to_owned();
    }
    let mut kept = String::new();
    for sentence in sentences(text) {
        if !kept.is_empty() && kept.chars().count() + sentence.chars().count() + 1 > MAX_DESCRIPTION {
            break;
        }
        kept = format!("{kept} {sentence}").trim().to_owned();
    }
    if kept.chars().count() > MAX_DESCRIPTION {
        let head: String = kept.chars().take(MAX_DESCRIPTION - 1).collect();
        kept = match head.rsplit_once(' ') {
            Some((before, _)) => before.to_owned(),
            None => head,
        };
    }
    kept + " (...)"
}

fn show_values(values: Option<&Value>) -> String {
    match values {
        None => String::new(),
        Some(Value::String(text)) => cell(text),
        Some(Value::Sequence(items)) => items
            .iter()
            .map(|item| code(&yaml::json(item)))
            .collect::<Vec<_>>()
            .join(", "),
        Some(other) => cell(&yaml::display(other)),
    }
}

/// Lines of output; `push` keeps the call sites short.
struct Out(Vec<String>);

impl Out {
    fn push(&mut self, line: impl Into<String>) {
        self.0.push(line.into());
    }
}

/// Render the whole reference.
pub fn render(roles: &[Role], annotations: &Annotations) -> String {
    let mut out = Out(Vec::new());
    header(&mut out);
    profiles(&mut out, roles, annotations);
    for role in roles {
        tags(&mut out, role, annotations);
    }
    variables(&mut out, roles, annotations);
    let mut text = out.0.join("\n");
    while text.ends_with('\n') {
        text.pop();
    }
    text.push('\n');
    text
}

fn header(out: &mut Out) {
    out.push("# Supported variable and tag reference");
    out.push("");
    out.push("<!-- Generated by `catalog-tools reference` (tools/). Do not edit by hand: change the role defaults,");
    out.push("     the role tasks or docs/reference-annotations.yml, then run `make reference`. -->");
    out.push("");
    out.push(
        "This is the public configuration surface of the `curiosity.workstation` collection: every role variable \
         with its type, default, valid values, stability and safety impact, the tags a focused run can select, \
         and the profile selectors. It is generated from the role defaults and tasks; CI fails when it is out of date \
         (`make reference-check`). [configuration.md](configuration.md) explains the options in prose.",
    );
    out.push("");
    out.push("## How to read this reference");
    out.push("");
    out.push("Stability:");
    out.push("");
    for (word, meaning) in STABILITY {
        out.push(format!("- **{word}**: {meaning}"));
    }
    out.push("");
    out.push(
        "Variables not marked otherwise are **supported**. The marking is curated in \
         [reference-annotations.yml](reference-annotations.yml); a variable missing from it is supported by default, \
         so review new variables when they are added.",
    );
    out.push("");
    out.push("Safety impact (a variable can carry several flags; blank means no special concern):");
    out.push("");
    for (flag, meaning) in SAFETY {
        out.push(format!("- **{flag}**: {meaning}"));
    }
    out.push("");
    out.push(
        "Defaults of dictionaries and long values are summarised here; read the role's `defaults/main.yml` for the full value.",
    );
    out.push("");
}

fn profiles(out: &mut Out, roles: &[Role], annotations: &Annotations) {
    out.push("## Profiles");
    out.push("");
    out.push(
        "Two independent selectors choose what a host converges. Any `workstation_profile` can be combined with any hardware profile.",
    );
    out.push("");
    let Some(role) = roles.iter().find(|role| role.name == "developer_workstation") else {
        out.push("");
        return;
    };
    if let (Some(values), Some(selector)) = (
        role.asserted.get("workstation_profile").filter(|values| !values.is_empty()),
        role.defaults.get("workstation_profile"),
    ) {
        out.push(format!(
            "- `workstation_profile` (default {}):",
            show_default("workstation_profile", &selector.value)
        ));
        let mut described: BTreeMap<String, String> = annotations.profile_notes.clone();
        for line in &selector.lines {
            if let Some(captures) = PROFILE_LINE.captures(line) {
                described.insert(captures[1].to_owned(), captures[2].to_owned());
            }
        }
        for value in values {
            let value = yaml::display(value);
            let text = described.get(&value).map_or("(not described)", String::as_str);
            out.push(format!("  - {}: {}", code(&value), cell(text)));
        }
    }
    if let Some(table) = role.defaults.get("hardware_profiles") {
        let empty = serde_norway::Mapping::new();
        let profiles = match &table.value {
            Value::Mapping(mapping) => mapping,
            _ => &empty,
        };
        let default = role
            .defaults
            .get("hardware_profile")
            .map(|selector| show_default("hardware_profile", &selector.value))
            .unwrap_or_else(|| "unset".to_owned());
        let names: Vec<String> = profiles.keys().map(|key| code(&yaml::display(key))).collect();
        out.push(format!(
            "- `hardware_profile` (default {default}): {}.",
            names.join(", ")
        ));
        out.push("");
        out.push("Settings each hardware profile defines in `hardware_profiles` (names only):");
        out.push("");
        out.push("| Hardware profile | Description | Settings |");
        out.push("| --- | --- | --- |");
        for (profile, body) in profiles {
            let (description, keys) = match body {
                Value::Mapping(body) => (
                    body.get("description").map(yaml::display).unwrap_or_default(),
                    body.keys()
                        .map(yaml::display)
                        .filter(|key| key != "description")
                        .map(|key| code(&key))
                        .collect::<Vec<_>>()
                        .join(", "),
                ),
                _ => (String::new(), String::new()),
            };
            out.push(format!(
                "| {} | {} | {keys} |",
                code(&yaml::display(profile)),
                cell(&description)
            ));
        }
    }
    out.push("");
}

fn tags(out: &mut Out, role: &Role, annotations: &Annotations) {
    out.push(format!("## Tags: `{}`", role.name));
    out.push("");
    if role.tags.is_empty() {
        out.push("This role defines no tags: a run includes all of its tasks or none of them.");
        out.push("");
        return;
    }
    out.push(
        "Select tags with `--tags` or exclude them with `--skip-tags`. A task carries a tag directly or through its \
         `block`. Tasks tagged `always` run on every invocation. The profiles column lists the \
         `workstation_profile` values named in the tasks' conditions; blank means the tag is not profile-gated.",
    );
    out.push("");
    out.push("| Tag | Status | Tasks | Task files | Profiles | Description |");
    out.push("| --- | --- | --- | --- | --- | --- |");
    for (tag, usage) in &role.tags {
        let note = annotations.tags.get(tag);
        let mut description = note.and_then(|note| note.description.clone()).unwrap_or_default();
        if tag == "always" && description.is_empty() {
            description = "Runs on every invocation; `--skip-tags always` is the only way to skip it.".to_owned();
        }
        let stability = note
            .and_then(|note| note.stability.as_deref())
            .unwrap_or("supported");
        let files: Vec<String> = usage.files.iter().map(|file| code(file)).collect();
        let profiles: Vec<String> = PROFILE_NAMES
            .iter()
            .filter(|name| usage.profiles.contains(*name))
            .map(|name| code(name))
            .collect();
        out.push(format!(
            "| {} | {stability} | {} | {} | {} | {} |",
            code(tag),
            usage.tasks,
            files.join(", "),
            profiles.join(", "),
            cell(&description)
        ));
    }
    out.push("");
}

fn variables(out: &mut Out, roles: &[Role], annotations: &Annotations) {
    out.push("## Variables");
    out.push("");
    let mut index: Vec<(usize, String, &str, String)> = Vec::new();
    for role in roles {
        for variable in role.defaults.iter() {
            let class = annotations.classify(&variable.name);
            if class.stability != "supported" {
                index.push((
                    stability_rank(&class.stability),
                    variable.name.clone(),
                    role.name.as_str(),
                    class.stability,
                ));
            }
        }
    }
    if !index.is_empty() {
        index.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
        out.push("### Not part of the supported surface");
        out.push("");
        out.push(
            "Variables marked experimental, internal or private-hook. Do not depend on internal ones; set private hooks only from protected host variables.",
        );
        out.push("");
        out.push("| Variable | Role | Status |");
        out.push("| --- | --- | --- |");
        for (_, name, role, stability) in &index {
            out.push(format!("| {} | {} | {stability} |", code(name), code(role)));
        }
        out.push("");
    }
    for role in roles {
        out.push(format!("### Role `{}`", role.name));
        out.push("");
        out.push(format!(
            "{} variables, from `roles/{}/defaults/main.yml`.",
            role.defaults.len(),
            role.name
        ));
        out.push("");
        out.push("| Variable | Type | Default | Valid values | Status | Safety impact | Description |");
        out.push("| --- | --- | --- | --- | --- | --- | --- |");
        for variable in role.defaults.iter() {
            let class = annotations.classify(&variable.name);
            let mut values = class
                .values
                .clone()
                .or_else(|| role.asserted.get(&variable.name).cloned().map(Value::Sequence));
            if variable.name == "hardware_profile" && values.as_ref().is_none_or(yaml::is_unset) {
                values = hardware_profile_names(role).or(values);
            }
            let mut description = summarize(&variable.description);
            if !class.note.is_empty() {
                if !description.is_empty() {
                    description.push(' ');
                }
                description.push_str(&class.note);
            }
            if description.is_empty() {
                description = "(undocumented)".to_owned();
            }
            out.push(format!(
                "| {} | {} | {} | {} | {} | {} | {} |",
                code(&variable.name),
                type_name(&variable.value),
                show_default(&variable.name, &variable.value),
                show_values(values.as_ref()),
                class.stability,
                class.safety.join(", "),
                cell(&description)
            ));
        }
        out.push("");
    }
}

/// The entries of `hardware_profiles`, the valid values of `hardware_profile`.
fn hardware_profile_names(role: &Role) -> Option<Value> {
    match role.defaults.get("hardware_profiles").map(|table| &table.value) {
        Some(Value::Mapping(table)) => Some(Value::Sequence(table.keys().cloned().collect())),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn value(text: &str) -> Value {
        yaml::parse(text, "test").unwrap()
    }

    #[test]
    fn defaults_are_summarised_and_redacted() {
        assert_eq!(show_default("x", &value("{a: 1, b: 2}")), "mapping, 2 keys");
        assert_eq!(show_default("x", &value("{a: 1}")), "mapping, 1 key");
        assert_eq!(show_default("wifi_psk", &value("hunter2")), "(redacted)");
        assert_eq!(show_default("wifi_psk", &value("''")), "`\"\"`");
        assert_eq!(show_default("x", &value("true")), "`true`");
        assert_eq!(show_default("x", &value("null")), "`null`");
        assert_eq!(show_default("x", &value("[a, b]")), "`[\"a\", \"b\"]`");
        assert_eq!(show_default("x", &value("\"a\\nb\"")), "multi-line text");
        let long = format!("[{}]", ["item"; 20].join(", "));
        assert_eq!(show_default("x", &value(&long)), "list, 20 items");
        let template = format!("\"{{{{ {} }}}}\"", "a".repeat(70));
        assert_eq!(show_default("x", &value(&template)), "template expression");
        let text = "z".repeat(70);
        assert_eq!(show_default("x", &value(&text)), "string, 70 characters");
    }

    #[test]
    fn type_names() {
        assert_eq!(type_name(&value("1")), "int");
        assert_eq!(type_name(&value("1.5")), "float");
        assert_eq!(type_name(&value("'{{ x }}'")), "string (template)");
        assert_eq!(type_name(&value("[]")), "list");
        assert_eq!(type_name(&value("{}")), "dict");
    }

    #[test]
    fn cells_and_code_spans_are_escaped() {
        assert_eq!(cell("a|b <c>\nd"), "a\\|b &lt;c&gt; d");
        assert_eq!(code("a|`b`"), "`a\\|'b'`");
    }

    #[test]
    fn long_descriptions_keep_whole_sentences() {
        let sentence = "This sentence is exactly fifty characters long ok.";
        let text = [sentence; 10].join(" ");
        let summary = summarize(&text);
        assert!(summary.ends_with(". (...)"), "{summary}");
        assert!(summary.chars().count() <= MAX_DESCRIPTION + 6);
        assert_eq!(summarize("Short."), "Short.");
        let word = "w".repeat(400);
        let cut = summarize(&word);
        assert!(cut.ends_with(" (...)") && cut.chars().count() <= MAX_DESCRIPTION + 6, "{cut}");
    }

    #[test]
    fn sentences_split_after_punctuation_and_space() {
        assert_eq!(sentences("A b. C d!  E? f.g"), ["A b.", "C d!", "E?", "f.g"]);
    }
}
