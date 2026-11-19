// Brand labels shown in the UI, in one place. See docs/NAMING.md: bee names on screen, plain names in code and logs.
// A rebrand is a change to this file only.
export const G = {
  attackers: "HORNETS", attackersSub: "US, ATTACKING",
  hive: "THE HIVE", hiveSub: "THE OLD BANK",
  colony: "THE COLONY",
  beekeeper: "THE BEEKEEPER", beekeeperSub: "OUTSIDE THE COLONY",
  memory: "comb",
};
// agent role → bee name (plain role is the subtitle)
export const ROLE = {
  coordinator: "Queen", investigator: "Scout", operator: "Forager", verifier: "Guard", monitor: "Sentinel",
  recovery: "Reserve", reporter: "Waggle", toolsmith: "Builder", upgrader: "Beekeeper",
};
export const PLAIN = {
  coordinator: "coordinator", investigator: "investigator", operator: "operator", verifier: "verifier", monitor: "monitor",
  recovery: "recovery", reporter: "reporter", toolsmith: "toolsmith", upgrader: "upgrader",
};
