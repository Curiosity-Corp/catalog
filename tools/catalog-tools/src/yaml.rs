//! YAML loading and the small value helpers the tools share.
//!
//! Ansible reads YAML with PyYAML; the role's YAML is linted (`.yamllint` forbids
//! implicit octal and truthy words), so a YAML 1.2 parser reads the same values.
//! Tags such as `!unsafe` only change how Ansible templates a string, so they
//! are dropped and the tagged value is kept.

use std::fs;
use std::path::Path;

use anyhow::{Context, Result};
use serde_norway::Value;

/// Parse YAML text; `origin` names the source in the error message.
pub fn parse(text: &str, origin: &str) -> Result<Value> {
    let value: Value =
        serde_norway::from_str(text).with_context(|| format!("{origin}: invalid YAML"))?;
    Ok(untag(value))
}

/// Read and parse a YAML file.
pub fn load(path: &Path) -> Result<Value> {
    let text = read(path)?;
    parse(&text, &path.display().to_string())
}

/// Read a UTF-8 text file with the path in the error message.
pub fn read(path: &Path) -> Result<String> {
    fs::read_to_string(path).with_context(|| format!("cannot read {}", path.display()))
}

/// Replace every tagged value by the value it tags, recursively.
pub fn untag(value: Value) -> Value {
    match value {
        Value::Tagged(tagged) => untag(tagged.value),
        Value::Sequence(items) => Value::Sequence(items.into_iter().map(untag).collect()),
        Value::Mapping(mapping) => Value::Mapping(
            mapping
                .into_iter()
                .map(|(key, value)| (untag(key), untag(value)))
                .collect(),
        ),
        other => other,
    }
}

/// A value as a list: nothing for null or absent, the items of a sequence, or
/// the value itself as the only item (Ansible accepts `tags: x` and `tags: [x]`).
pub fn as_list(value: Option<&Value>) -> Vec<&Value> {
    match value {
        None | Some(Value::Null) => Vec::new(),
        Some(Value::Sequence(items)) => items.iter().collect(),
        Some(other) => vec![other],
    }
}

/// The text of a scalar the way Ansible shows it (Python's `str`). Collections
/// are shown as JSON.
pub fn display(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Bool(true) => "True".to_owned(),
        Value::Bool(false) => "False".to_owned(),
        Value::Null => "None".to_owned(),
        Value::Number(number) => number_text(number),
        other => json(other),
    }
}

/// Compact JSON with Python's `json.dumps` spacing (`", "` and `": "`); non-ASCII
/// text is kept as is.
pub fn json(value: &Value) -> String {
    match value {
        Value::Null => "null".to_owned(),
        Value::Bool(flag) => flag.to_string(),
        Value::Number(number) => number_text(number),
        Value::String(text) => string_literal(text),
        Value::Sequence(items) => {
            let parts: Vec<String> = items.iter().map(json).collect();
            format!("[{}]", parts.join(", "))
        }
        Value::Mapping(mapping) => {
            let parts: Vec<String> = mapping
                .iter()
                .map(|(key, value)| format!("{}: {}", string_literal(&display(key)), json(value)))
                .collect();
            format!("{{{}}}", parts.join(", "))
        }
        Value::Tagged(tagged) => json(&tagged.value),
    }
}

fn string_literal(text: &str) -> String {
    // Serialising a plain string cannot fail.
    serde_json::to_string(text).unwrap_or_else(|_| format!("\"{text}\""))
}

fn number_text(number: &serde_norway::Number) -> String {
    if let Some(integer) = number.as_i64() {
        return integer.to_string();
    }
    if let Some(integer) = number.as_u64() {
        return integer.to_string();
    }
    match number.as_f64() {
        Some(float) if float.is_finite() && float.fract() == 0.0 && float.abs() < 1e16 => {
            format!("{float:.1}")
        }
        Some(float) => float.to_string(),
        None => number.to_string(),
    }
}

/// True for an integer value (YAML `1`), false for a float (`1.0`).
pub fn is_integer(number: &serde_norway::Number) -> bool {
    number.is_i64() || number.is_u64()
}

/// Python's falsy scalars that the reference treats as "nothing set": `""`,
/// null, `[]`, `false` and zero.
pub fn is_unset(value: &Value) -> bool {
    match value {
        Value::Null => true,
        Value::String(text) => text.is_empty(),
        Value::Sequence(items) => items.is_empty(),
        Value::Bool(flag) => !flag,
        Value::Number(number) => number.as_f64() == Some(0.0),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn value(text: &str) -> Value {
        parse(text, "test").expect("valid YAML")
    }

    #[test]
    fn unsafe_tag_is_dropped() {
        assert_eq!(value("!unsafe '{{ x }}'"), Value::String("{{ x }}".into()));
    }

    #[test]
    fn json_uses_python_spacing() {
        assert_eq!(
            json(&value("[a, 1, 2.5, true, null]")),
            r#"["a", 1, 2.5, true, null]"#
        );
        assert_eq!(json(&value("{a: 1}")), r#"{"a": 1}"#);
        assert_eq!(json(&value("1.0")), "1.0");
        assert_eq!(json(&value("'café'")), "\"café\"");
    }

    #[test]
    fn as_list_accepts_scalar_and_sequence() {
        let scalar = value("x");
        assert_eq!(as_list(Some(&scalar)).len(), 1);
        let list = value("[a, b]");
        assert_eq!(as_list(Some(&list)).len(), 2);
        assert!(as_list(None).is_empty());
        assert!(as_list(Some(&Value::Null)).is_empty());
    }

    #[test]
    fn unset_values() {
        for text in ["''", "null", "[]", "false", "0", "0.0"] {
            assert!(is_unset(&value(text)), "{text} should be unset");
        }
        for text in ["x", "[1]", "true", "1", "{a: 1}"] {
            assert!(!is_unset(&value(text)), "{text} should be set");
        }
    }

    #[test]
    fn invalid_yaml_names_its_origin() {
        let error = parse("a: [", "roles/x/defaults/main.yml").unwrap_err();
        assert!(format!("{error:#}").contains("roles/x/defaults/main.yml"));
    }
}
