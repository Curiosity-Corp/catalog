//! Role tasks: the tags a run can select and the values `assert` tasks allow.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};
use regex::Regex;
use serde_norway::Value;

use super::defaults::Defaults;
use crate::yaml;

/// The `workstation_profile` values, in the order the reference lists them.
pub const PROFILE_NAMES: [&str; 4] = ["desktop", "workspace", "thin-client", "byod-kiosk"];

static TAG_VARIABLE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}$").expect("valid regex"));
/// `VAR in [...]`, optionally through filters (`VAR | string in [...]`).
static ASSERT_IN: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)(?:\s*\|\s*[A-Za-z_]+(?:\([^)]*\))?)*\s+in\s+(\[.*\])\s*$",
    )
    .expect("valid regex")
});

/// Where a tag is used.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct TagUse {
    /// Task file names (not paths) that carry the tag.
    pub files: BTreeSet<String>,
    /// Number of tasks (blocks included) that carry the tag.
    pub tasks: usize,
    /// `workstation_profile` values named in the conditions of those tasks.
    pub profiles: BTreeSet<&'static str>,
}

/// A parsed task file.
pub struct TaskFile {
    pub name: String,
    pub tasks: Value,
}

/// A task with what it inherits from the blocks around it.
struct Visit<'a> {
    task: &'a serde_norway::Mapping,
    inherited_tags: Vec<String>,
    conditions: Vec<String>,
}

/// Load `tasks/*.yml` of a role, sorted by file name.
pub fn load_task_files(role: &Path) -> Result<Vec<TaskFile>> {
    let directory = role.join("tasks");
    if !directory.is_dir() {
        return Ok(Vec::new());
    }
    let mut paths: Vec<PathBuf> = std::fs::read_dir(&directory)
        .with_context(|| format!("cannot list {}", directory.display()))?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<std::io::Result<_>>()
        .with_context(|| format!("cannot list {}", directory.display()))?;
    paths.retain(|path| path.is_file() && path.extension().is_some_and(|ext| ext == "yml"));
    paths.sort();
    paths
        .into_iter()
        .map(|path| {
            let tasks = yaml::load(&path)?;
            if !matches!(tasks, Value::Null | Value::Sequence(_)) {
                bail!("{}: a task file must be a list of tasks", path.display());
            }
            let name = path
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default();
            Ok(TaskFile { name, tasks })
        })
        .collect()
}

fn visit<'a>(
    tasks: Option<&'a Value>,
    inherited_tags: &[String],
    conditions: &[String],
    out: &mut Vec<Visit<'a>>,
) {
    let Some(Value::Sequence(items)) = tasks else {
        return;
    };
    for item in items {
        let Value::Mapping(task) = item else {
            continue;
        };
        out.push(Visit {
            task,
            inherited_tags: inherited_tags.to_vec(),
            conditions: conditions.to_vec(),
        });
        let mut tags = inherited_tags.to_vec();
        tags.extend(
            yaml::as_list(task.get("tags"))
                .into_iter()
                .map(yaml::display),
        );
        let mut when = conditions.to_vec();
        when.extend(
            yaml::as_list(task.get("when"))
                .into_iter()
                .map(yaml::display),
        );
        for key in ["block", "rescue", "always"] {
            visit(task.get(key), &tags, &when, out);
        }
    }
}

/// Every task of a file, nested blocks included.
fn walk(file: &TaskFile) -> Vec<Visit<'_>> {
    let mut out = Vec::new();
    visit(Some(&file.tasks), &[], &[], &mut out);
    out
}

/// Flatten tag entries; `"{{ variable }}"` stands for that variable's default list.
pub fn resolve_tags(raw: &[String], defaults: &Defaults) -> Result<Vec<String>> {
    let mut tags = Vec::new();
    for item in raw {
        match TAG_VARIABLE.captures(item) {
            Some(captures) => {
                let Some(variable) = defaults.get(&captures[1]) else {
                    bail!("tag expression {item:?} names a variable that has no default");
                };
                tags.extend(
                    yaml::as_list(Some(&variable.value))
                        .into_iter()
                        .map(yaml::display),
                );
            }
            None => tags.push(item.clone()),
        }
    }
    Ok(tags)
}

/// The profiles a set of conditions restricts a task to.
fn profiles_named(conditions: &[String]) -> BTreeSet<&'static str> {
    conditions
        .iter()
        .filter(|condition| condition.contains("workstation_profile"))
        .flat_map(|condition| {
            PROFILE_NAMES.into_iter().filter(move |name| {
                condition.contains(&format!("'{name}'"))
                    || condition.contains(&format!("\"{name}\""))
            })
        })
        .collect()
}

/// The tags of a role: which files and how many tasks carry each, and which
/// profiles their conditions name.
pub fn collect_tags(files: &[TaskFile], defaults: &Defaults) -> Result<BTreeMap<String, TagUse>> {
    let mut tags: BTreeMap<String, TagUse> = BTreeMap::new();
    for file in files {
        for visit in walk(file) {
            let own: Vec<String> = yaml::as_list(visit.task.get("tags"))
                .into_iter()
                .map(yaml::display)
                .collect();
            let mut effective: BTreeSet<String> = resolve_tags(&visit.inherited_tags, defaults)
                .with_context(|| format!("tasks/{}", file.name))?
                .into_iter()
                .collect();
            effective.extend(
                resolve_tags(&own, defaults).with_context(|| format!("tasks/{}", file.name))?,
            );
            if effective.is_empty() {
                continue;
            }
            let mut when = visit.conditions.clone();
            when.extend(
                yaml::as_list(visit.task.get("when"))
                    .into_iter()
                    .map(yaml::display),
            );
            let profiles = profiles_named(&when);
            for tag in effective {
                let entry = tags.entry(tag).or_default();
                entry.files.insert(file.name.clone());
                entry.tasks += 1;
                entry.profiles.extend(profiles.iter().copied());
            }
        }
    }
    Ok(tags)
}

/// Valid values that `assert` tasks state as `VAR in [...]`, first statement wins.
pub fn collect_asserted_values(
    files: &[TaskFile],
    defaults: &Defaults,
) -> HashMap<String, Vec<Value>> {
    let mut found: HashMap<String, Vec<Value>> = HashMap::new();
    for file in files {
        for visit in walk(file) {
            let module = visit
                .task
                .get("ansible.builtin.assert")
                .or_else(|| visit.task.get("assert"));
            let Some(Value::Mapping(module)) = module else {
                continue;
            };
            for condition in yaml::as_list(module.get("that")) {
                let condition = yaml::display(condition);
                let Some(captures) = ASSERT_IN.captures(&condition) else {
                    continue;
                };
                let name = &captures[1];
                if !defaults.contains(name) || found.contains_key(name) {
                    continue;
                }
                // A list that is not literal YAML (a Jinja expression) states no fixed values.
                if let Ok(Value::Sequence(values)) = yaml::parse(&captures[2], "assert") {
                    found.insert(name.to_owned(), values);
                }
            }
        }
    }
    found
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reference::defaults;

    fn file(name: &str, text: &str) -> TaskFile {
        TaskFile {
            name: name.to_owned(),
            tasks: yaml::parse(text, name).unwrap(),
        }
    }

    fn defaults() -> Defaults {
        defaults::parse(
            "profile: desktop\nupdate_tags: [updates, always]\nmode: a\n",
            "defaults",
        )
        .unwrap()
    }

    #[test]
    fn tags_are_inherited_from_blocks_and_resolved_from_variables() {
        let files = [file(
            "main.yml",
            "
- name: Block
  tags: [outer]
  when: profile == 'desktop'
  block:
    - name: Inner
      ansible.builtin.debug: {msg: hi}
      tags: inner
    - name: Untagged inner
      ansible.builtin.debug: {msg: hi}
- name: Transaction
  ansible.builtin.include_tasks: x.yml
  tags: '{{ update_tags }}'
- name: Plain
  ansible.builtin.debug: {msg: hi}
",
        )];
        let tags = collect_tags(&files, &defaults()).unwrap();
        let names: Vec<&str> = tags.keys().map(String::as_str).collect();
        assert_eq!(names, ["always", "inner", "outer", "updates"]);
        assert_eq!(tags["outer"].tasks, 3, "the block and both inner tasks");
        assert_eq!(tags["inner"].tasks, 1);
        assert_eq!(tags["updates"].tasks, 1);
        assert!(tags["outer"].files.contains("main.yml"));
    }

    #[test]
    fn profiles_come_from_workstation_profile_conditions() {
        let files = [file(
            "kiosk.yml",
            "
- name: Kiosk only
  ansible.builtin.debug: {msg: hi}
  tags: kiosk
  when:
    - workstation_profile in ['byod-kiosk', \"thin-client\"]
    - other == 'desktop'
",
        )];
        let tags = collect_tags(&files, &defaults()).unwrap();
        let profiles: Vec<&str> = tags["kiosk"].profiles.iter().copied().collect();
        assert_eq!(profiles, ["byod-kiosk", "thin-client"]);
    }

    #[test]
    fn unknown_tag_variable_is_an_error() {
        let files = [file(
            "x.yml",
            "- name: X\n  tags: '{{ missing }}'\n  ansible.builtin.debug: {}\n",
        )];
        let error = collect_tags(&files, &defaults()).unwrap_err();
        assert!(format!("{error:#}").contains("missing"), "{error:#}");
    }

    #[test]
    fn asserted_values_are_collected() {
        let files = [file(
            "validate.yml",
            "
- name: Validate
  ansible.builtin.assert:
    that:
      - mode in ['a', 'b']
      - profile | string in [desktop, workspace]
      - unknown in [1, 2]
      - mode in some_list
- name: Later statements do not override
  assert:
    that: mode in ['z']
",
        )];
        let found = collect_asserted_values(&files, &defaults());
        assert_eq!(found["mode"], vec![Value::from("a"), Value::from("b")]);
        assert_eq!(found["profile"].len(), 2);
        assert!(!found.contains_key("unknown"));
    }
}
