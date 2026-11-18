# caa_java_mutation
java sample repo for mutation

## Layout

PIT (mutation testing) is **not** declared in this repo's own poms. It comes from a parent pom's
`mutation-testing` profile, the way a team or platform parent usually provides it:

```
parents/platform-parent/pom.xml   profile "mutation-testing" -> pitest-maven + JUnit 5 plugin
pom.xml                           calculator-parent, inherits platform-parent (relativePath)
impl/pom.xml                      the module; no pitest here
```

This is the case fixed in `unified-mutation-testing` 2.0.0-rc12 (PR #13 in
`NiCE-AI-Marketplace/quality-agentic-platform`): rc9 stops with "pitest for java is not installed"
on this layout, rc12 finds PIT through the parent chain.

## Run

    mvn -B test
    mvn -B -P mutation-testing org.pitest:pitest-maven:mutationCoverage

The profile is required: without `-P mutation-testing` no PIT plugin is in the build.
