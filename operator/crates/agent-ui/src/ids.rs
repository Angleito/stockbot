//! Opaque identifier wrappers.
//!
//! Every id is a distinct newtype over [`String`] so call sites cannot mix
//! up agents, workers, artifacts, and log entries. There is intentionally no
//! domain meaning attached to any of them.

macro_rules! define_id {
    ($($name:ident),*) => {
        $(
            #[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
            pub struct $name(String);

            impl $name {
                /// Wrap a raw string without validating it.
                pub fn new(raw: impl Into<String>) -> Self {
                    Self(raw.into())
                }

                /// Borrow the raw value.
                pub fn as_str(&self) -> &str {
                    &self.0
                }
            }

            impl std::fmt::Display for $name {
                fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                    f.write_str(&self.0)
                }
            }

            impl From<String> for $name {
                fn from(raw: String) -> Self {
                    Self(raw)
                }
            }

            impl From<&str> for $name {
                fn from(raw: &str) -> Self {
                    Self(raw.to_owned())
                }
            }
        )*
    };
}

define_id!(AgentId, WorkerId, ArtifactId, EventId);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_do_not_mix() {
        let agent = AgentId::new("a-1");
        let worker = WorkerId::from("a-1");
        assert_eq!(agent.as_str(), worker.as_str());
        assert_eq!(agent.to_string(), "a-1");
        assert_eq!(EventId::from("e-7"), EventId::new("e-7"));
        assert_eq!(ArtifactId::from(String::from("x")), ArtifactId::new("x"));
    }
}
