//! CycloneDX 1.6 components and the document around them.

use std::collections::BTreeSet;

use anyhow::{Result, bail};
use serde_json::{Map, Value as Json, json};

use super::Inventory;

/// The property every component carries to say what it is.
pub const KIND_PROPERTY: &str = "curiosity:component-kind";

/// What a component is, in the order the SBOM lists them.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub enum Kind {
    #[default]
    SourceFile,
    CollectionDependency,
    RuntimePackageSource,
    RuntimePackage,
    ExternalInstaller,
    CiTool,
}

impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Kind::SourceFile => "source-file",
            Kind::CollectionDependency => "collection-dependency",
            Kind::RuntimePackageSource => "runtime-package-source",
            Kind::RuntimePackage => "runtime-package",
            Kind::ExternalInstaller => "external-installer",
            Kind::CiTool => "ci-tool",
        }
    }

    /// CycloneDX scope: files and collection dependencies ship with or are
    /// required by the collection; runtime sources are used only when the
    /// feature that needs them is enabled; CI tools never reach a host.
    fn scope(self) -> &'static str {
        match self {
            Kind::SourceFile | Kind::CollectionDependency => "required",
            Kind::RuntimePackageSource | Kind::RuntimePackage | Kind::ExternalInstaller => {
                "optional"
            }
            Kind::CiTool => "excluded",
        }
    }

    /// Whether the collection itself depends on components of this kind.
    fn is_dependency(self) -> bool {
        !matches!(self, Kind::SourceFile | Kind::CiTool)
    }
}

/// An external reference (`externalReferences` entry).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Reference {
    pub kind: &'static str,
    pub url: String,
    pub comment: Option<String>,
}

/// One CycloneDX component.
#[derive(Clone, Debug, Default)]
pub struct Component {
    pub bom_ref: String,
    pub kind: Kind,
    pub component_type: String,
    pub group: Option<String>,
    pub name: String,
    pub version: Option<String>,
    pub description: Option<String>,
    pub purl: Option<String>,
    /// Lower-case hexadecimal SHA-256 digests.
    pub hashes: Vec<String>,
    pub references: Vec<Reference>,
    pub properties: Vec<(String, String)>,
}

impl Component {
    pub fn to_json(&self) -> Json {
        let mut object = Map::new();
        object.insert("bom-ref".into(), json!(self.bom_ref));
        object.insert("type".into(), json!(self.component_type));
        if let Some(group) = &self.group {
            object.insert("group".into(), json!(group));
        }
        object.insert("name".into(), json!(self.name));
        if let Some(version) = &self.version {
            object.insert("version".into(), json!(version));
        }
        if let Some(description) = &self.description {
            object.insert("description".into(), json!(description));
        }
        object.insert("scope".into(), json!(self.kind.scope()));
        if !self.hashes.is_empty() {
            let hashes: Vec<Json> = self
                .hashes
                .iter()
                .map(|digest| json!({"alg": "SHA-256", "content": digest}))
                .collect();
            object.insert("hashes".into(), Json::Array(hashes));
        }
        if let Some(purl) = &self.purl {
            object.insert("purl".into(), json!(purl));
        }
        if !self.references.is_empty() {
            let references: Vec<Json> = self
                .references
                .iter()
                .map(|reference| {
                    let mut entry = Map::new();
                    entry.insert("type".into(), json!(reference.kind));
                    entry.insert("url".into(), json!(reference.url));
                    if let Some(comment) = &reference.comment {
                        entry.insert("comment".into(), json!(comment));
                    }
                    Json::Object(entry)
                })
                .collect();
            object.insert("externalReferences".into(), Json::Array(references));
        }
        let mut properties = vec![json!({"name": KIND_PROPERTY, "value": self.kind.as_str()})];
        properties.extend(
            self.properties
                .iter()
                .map(|(name, value)| json!({"name": name, "value": value})),
        );
        object.insert("properties".into(), Json::Array(properties));
        Json::Object(object)
    }
}

/// The fields that identify a component for [`super::check`]: a dependency
/// that changed version, checksum, source or kind no longer matches.
pub fn identity(component: &Json) -> Json {
    let mut object = Map::new();
    for key in [
        "type", "group", "name", "version", "scope", "purl", "hashes",
    ] {
        if let Some(value) = component.get(key) {
            object.insert(key.into(), value.clone());
        }
    }
    let kind = component
        .get("properties")
        .and_then(Json::as_array)
        .and_then(|properties| {
            properties
                .iter()
                .find(|property| property.get("name") == Some(&json!(KIND_PROPERTY)))
        })
        .and_then(|property| property.get("value"))
        .cloned()
        .unwrap_or(Json::Null);
    object.insert("kind".into(), kind);
    Json::Object(object)
}

/// Kind order, then bom-ref.
pub fn sort(components: &mut [Component]) {
    components.sort_by(|a, b| (a.kind, &a.bom_ref).cmp(&(b.kind, &b.bom_ref)));
}

/// CycloneDX requires every bom-ref to be unique.
pub fn ensure_unique(components: &[Component]) -> Result<()> {
    let mut seen = BTreeSet::new();
    for component in components {
        if !seen.insert(component.bom_ref.as_str()) {
            bail!(
                "two SBOM components share the bom-ref {}",
                component.bom_ref
            );
        }
    }
    Ok(())
}

/// The whole document.
pub fn render(inventory: &Inventory, timestamp: &str) -> Json {
    let collection = &inventory.collection;
    let root_ref = format!("collection:{}.{}", collection.namespace, collection.name);
    let mut root = json!({
        "bom-ref": root_ref,
        "type": "application",
        "group": collection.namespace,
        "name": collection.name,
        "version": collection.version,
        "purl": format!(
            "pkg:generic/{}/{}@{}",
            collection.namespace, collection.name, collection.version
        ),
        "hashes": [{"alg": "SHA-256", "content": collection.archive_sha256}],
    });
    if !collection.license.is_empty() {
        root["licenses"] = json!([{"expression": collection.license.join(" OR ")}]);
    }
    let depends_on: Vec<&str> = inventory
        .components
        .iter()
        .filter(|component| component.kind.is_dependency())
        .map(|component| component.bom_ref.as_str())
        .collect();
    let components: Vec<Json> = inventory
        .components
        .iter()
        .map(Component::to_json)
        .collect();
    json!({
        "$schema": "http://cyclonedx.org/schema/bom-1.6.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": serial_number(&collection.archive_sha256),
        "version": 1,
        "metadata": {
            "timestamp": timestamp,
            "tools": {
                "components": [{
                    "type": "application",
                    "name": "catalog-tools",
                    "version": env!("CARGO_PKG_VERSION"),
                    "description": "catalog-tools sbom (tools/README.md)",
                }],
            },
            "component": root,
            "properties": [
                {"name": "curiosity:sources", "value": super::SOURCES},
                {"name": "curiosity:documentation", "value": "docs/sbom.md"},
            ],
        },
        "components": components,
        "dependencies": [{"ref": root_ref, "dependsOn": depends_on}],
    })
}

/// A stable RFC 4122 shaped URN derived from the artifact digest, so the same
/// artifact always gets the same serial number.
pub fn serial_number(archive_sha256: &str) -> String {
    let mut hex: Vec<char> = archive_sha256.chars().take(32).collect();
    hex.resize(32, '0');
    hex[12] = '5';
    let variant = hex[16].to_digit(16).unwrap_or(0);
    hex[16] = char::from_digit(8 | (variant & 0x3), 16).unwrap_or('8');
    let text: String = hex.into_iter().collect();
    format!(
        "urn:uuid:{}-{}-{}-{}-{}",
        &text[0..8],
        &text[8..12],
        &text[12..16],
        &text[16..20],
        &text[20..32]
    )
}

/// `seconds` since the epoch as an RFC 3339 UTC timestamp.
pub fn rfc3339(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    let rest = seconds.rem_euclid(86_400);
    // Civil date from days since 1970-01-01 (H. Hinnant, "chrono-Compatible
    // Low-Level Date Algorithms").
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let day_of_era = z.rem_euclid(146_097);
    let year_of_era =
        (day_of_era - day_of_era / 1460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rest / 3600,
        rest % 3600 / 60,
        rest % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamps() {
        assert_eq!(rfc3339(0), "1970-01-01T00:00:00Z");
        assert_eq!(rfc3339(951_782_400), "2000-02-29T00:00:00Z");
        assert_eq!(rfc3339(1_795_651_199), "2026-11-25T23:59:59Z");
    }

    #[test]
    fn serial_number_is_uuid_shaped_and_stable() {
        let digest = "10dffc7c73d5ef367fefb005dfff4528aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let serial = serial_number(digest);
        assert_eq!(serial, "urn:uuid:10dffc7c-73d5-5f36-bfef-b005dfff4528");
        assert_eq!(serial, serial_number(digest));
    }

    #[test]
    fn identity_ignores_descriptive_fields() {
        let component = Component {
            bom_ref: "source:x".into(),
            kind: Kind::RuntimePackage,
            component_type: "library".into(),
            name: "x".into(),
            version: Some("1".into()),
            description: Some("words".into()),
            ..Component::default()
        };
        let mut changed = component.clone();
        changed.description = Some("other words".into());
        assert_eq!(identity(&component.to_json()), identity(&changed.to_json()));
        changed.version = Some("2".into());
        assert_ne!(identity(&component.to_json()), identity(&changed.to_json()));
    }
}
