//! Role defaults: each top-level variable with its value and the comment above it.

use std::collections::HashMap;
use std::sync::LazyLock;

use anyhow::{Result, bail};
use regex::Regex;
use serde_norway::Value;

use crate::yaml;

static TOP_KEY: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s|$)").expect("valid regex"));
/// A banner line such as `# ======` closes a section heading.
static RULE_LINE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[=\-#*_ ]{4,}$").expect("valid regex"));

/// One variable from `defaults/main.yml`.
#[derive(Debug, Clone)]
pub struct Variable {
    pub name: String,
    pub value: Value,
    /// Non-empty comment lines directly above the variable, without `#`.
    pub lines: Vec<String>,
    /// The comment lines joined into one paragraph.
    pub description: String,
}

/// The variables of one role, in file order.
#[derive(Debug, Default)]
pub struct Defaults {
    variables: Vec<Variable>,
    index: HashMap<String, usize>,
}

impl Defaults {
    pub fn iter(&self) -> impl Iterator<Item = &Variable> {
        self.variables.iter()
    }

    pub fn get(&self, name: &str) -> Option<&Variable> {
        self.index.get(name).map(|&position| &self.variables[position])
    }

    pub fn contains(&self, name: &str) -> bool {
        self.index.contains_key(name)
    }

    pub fn len(&self) -> usize {
        self.variables.len()
    }

    pub fn is_empty(&self) -> bool {
        self.variables.is_empty()
    }

    pub fn names(&self) -> impl Iterator<Item = &str> {
        self.variables.iter().map(|variable| variable.name.as_str())
    }
}

/// Parse a defaults file. The description of a variable is the unindented
/// comment block directly above it, after the last banner line.
pub fn parse(text: &str, origin: &str) -> Result<Defaults> {
    let values = match yaml::parse(text, origin)? {
        Value::Null => serde_norway::Mapping::new(),
        Value::Mapping(mapping) => mapping,
        _ => bail!("{origin}: a defaults file must be a mapping of variable names"),
    };
    let lines: Vec<&str> = text.lines().collect();
    let mut comments: Vec<(String, Vec<String>)> = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        let Some(captures) = TOP_KEY.captures(line) else {
            continue;
        };
        let mut block: Vec<String> = Vec::new();
        let mut cursor = index;
        while cursor > 0 {
            let above = lines[cursor - 1];
            if above.starts_with(' ') || !above.trim_start().starts_with('#') {
                break;
            }
            block.push(above.trim_start().trim_start_matches('#').trim().to_owned());
            cursor -= 1;
        }
        block.reverse();
        if let Some(rule) = block.iter().rposition(|part| RULE_LINE.is_match(part)) {
            block.drain(..=rule);
        }
        block.retain(|part| !part.is_empty());
        comments.push((captures[1].to_owned(), block));
    }

    let yaml_names: Vec<String> = values.keys().map(yaml::display).collect();
    let comment_names: Vec<&str> = comments.iter().map(|(name, _)| name.as_str()).collect();
    if yaml_names != comment_names {
        bail!(
            "{origin}: the line scanner and the YAML parser disagree about the top-level variables; \
             write each default as an unindented `name: value` line"
        );
    }

    let mut defaults = Defaults::default();
    for ((name, lines), (_, value)) in comments.into_iter().zip(values) {
        let description = lines.join(" ").trim().to_owned();
        defaults.index.insert(name.clone(), defaults.variables.len());
        defaults.variables.push(Variable {
            name,
            value,
            lines,
            description,
        });
    }
    Ok(defaults)
}

#[cfg(test)]
mod tests {
    use super::*;

    const TEXT: &str = "---
# =====
# Section heading
# =====
# The first variable.
# It has two lines.
first: 1

second: [a, b]
# Not attached to anything above.

# Third.
third:
  nested: true
  # indented comment
";

    #[test]
    fn descriptions_come_from_the_comment_above() {
        let defaults = parse(TEXT, "test").unwrap();
        assert_eq!(defaults.len(), 3);
        let first = defaults.get("first").unwrap();
        assert_eq!(first.description, "The first variable. It has two lines.");
        assert_eq!(defaults.get("second").unwrap().description, "");
        assert_eq!(defaults.get("third").unwrap().description, "Third.");
        let names: Vec<&str> = defaults.names().collect();
        assert_eq!(names, ["first", "second", "third"]);
    }

    #[test]
    fn a_flow_mapping_on_one_line_is_rejected() {
        let error = parse("{a: 1, b: 2}\n", "flow.yml").unwrap_err();
        assert!(error.to_string().contains("disagree"), "{error}");
    }

    #[test]
    fn a_list_is_rejected() {
        assert!(parse("- a\n", "list.yml").is_err());
    }

    #[test]
    fn empty_file_has_no_variables() {
        assert!(parse("---\n", "empty.yml").unwrap().is_empty());
    }
}
