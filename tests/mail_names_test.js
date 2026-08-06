/**
 * Name filtering: does folding actually catch the family that walked through the plain check,
 * and does it leave ordinary names alone?
 *
 * The second half matters as much as the first. A filter that refuses "assistant" is not strict,
 * it is broken, and the person it inconveniences is never the one it was aimed at.
 *
 *   node tests/mail_names_test.js
 */
import { _names } from "../relay/mail_worker.js";

const { foldName, blockedName } = _names;

// `fack` and `fck` are in the WORD list, not handled by folding: the fold only undoes LOOKALIKE
// substitutions (1→i, 0→o, 3→e). In "f4ck" the 4 stands in for a "u", which is not a lookalike at
// all — no amount of folding turns it into "fuck". Variants like that are data, and belong in the
// list rather than in a cleverer matcher.
const list = {
  words: ["fuck", "fack", "fck", "shit", "nigg", "ass", "official", "verify"],
  allow: ["assistant", "analysis", "class", "shitake", "verifier"],
};
const env = { DIRECTORY: { get: async () => list } };

let fails = 0;
async function check(name, shouldBlock, why) {
  const got = !!(await blockedName(env, name));
  const ok = got === shouldBlock;
  if (!ok) fails++;
  console.log(`  ${ok ? "PASS" : "FAIL"} ${name.padEnd(16)} ${shouldBlock ? "blocked" : "allowed"} — ${why}`);
}

console.log("fold: n1gger ->", foldName("n1gger"), "· fuuuck ->", foldName("fuuuck"),
            "· a$$hole ->", foldName("a$$hole"), "· assistant ->", foldName("assistant"));

// the family that used to walk straight through
await check("n1gger", true, "digit for a letter");
await check("f4ck", true, "leetspeak");
await check("fuuuuck", true, "stretched");
await check("sh1t", true, "digit inside");
await check("0fficial", true, "zero for o");
await check("fuck-collie", true, "punctuation is not a disguise");

// and the ordinary names it must not touch
await check("assistant", false, "folds to asistant — no longer contains ass");
await check("analysis", false, "on the allow list");
await check("shitake", false, "folds into a blocked word, explicitly permitted");
await check("class", false, "ordinary word");
await check("rowan", false, "a dog");
await check("daming", false, "a person");
await check("verifier", false, "allowed even though it contains verify");

console.log(fails ? `\n  ${fails} FAILED` : "\n  name filter: all green");
process.exit(fails ? 1 : 0);
