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
  queen: "Queen", scout: "Scout", worker: "Forager", guard: "Guard", sentinel: "Sentinel",
  nurse: "Reserve", waggle: "Waggle", platform: "Builder", gardener: "Beekeeper",
};
export const PLAIN = {
  queen: "coordinator", scout: "investigator", worker: "operator", guard: "verifier", sentinel: "monitor",
  nurse: "recovery", waggle: "reporter", platform: "toolsmith", gardener: "upgrader",
};
