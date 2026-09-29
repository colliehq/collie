/**
 * The Worker and the Python client are two halves of one protocol. This is the check that they
 * agree on the bytes — key derivation, MAC input framing, and the sealed envelope — because
 * "it looks right in both files" is exactly how a wire format forks silently and is discovered
 * later by a message nobody can open.
 *
 * Driven from Python (tests/test_dogmail_wire.py): that side writes a fixture, this side answers
 * with what the Worker's code produces, and Python compares.
 *
 *   node tests/mail_crossimpl_test.js <fixture.json> <out.json>
 */
import { readFileSync, writeFileSync } from "node:fs";
import { _crypto, _send } from "../relay/mail_worker.js";

const [, , fixturePath, outPath] = process.argv;
const fx = JSON.parse(readFileSync(fixturePath, "utf8"));
const { lp, cat, x25519, hkdf, hmac, sealToDog, b64, ub64 } = _crypto;
const enc = new TextEncoder();

const relayPriv = ub64(fx.relay_priv);
const dogPub = ub64(fx.dog_pub);
const handlePub = ub64(fx.handle_pub);

// 1. the auth key + stamp the Worker would expect from this dog
const authKey = await hkdf(await x25519(relayPriv, dogPub),
                           enc.encode(fx.address), enc.encode("collie-mail-auth"));
const mac = await hmac(authKey, cat(lp(fx.method), lp(fx.path), lp(fx.ts), lp(fx.nonce)));

// 2. the cert tag the Worker would expect from this handle
const certKey = await hkdf(await x25519(relayPriv, handlePub),
                           enc.encode("handle"), enc.encode("collie-mail-cert"));
const cert = await hmac(certKey, cat(lp(fx.address), lp(dogPub)));

// 3. an envelope sealed the way delivery seals it, for Python to open
const sealed = await sealToDog(dogPub, enc.encode(fx.plaintext));

// 4. a send body the Python client built: validated by the Worker, then built into the raw legacy
//    message and the structured builder call, for Python's own mail parser to read back
const out = { mac: b64(mac), cert: b64(cert), sealed };
if (fx.send_body) {
  const { msg, error } = _send.validateSend(fx.send_body, fx.owner);
  out.send_error = error || null;
  if (msg) {
    const calls = [];
    const mailer = { send: async (message) => { calls.push(message); return undefined; } };
    class RawMessage { constructor(from, to, raw) { Object.assign(this, { from, to, raw }); } }
    await _send.deliver({ MAIL_SEND_MODE: "legacy", MAIL_DOMAIN: "collie.run", MAILER: mailer,
                          EMAIL_MESSAGE: RawMessage }, fx.address, msg.to, msg, "send.0123abcd");
    await _send.deliver({ MAILER: mailer }, fx.address, msg.to, msg, "send.0123abcd");
    out.legacy_raw = calls[0].raw;
    out.structured = calls[1];
  }
}

writeFileSync(outPath, JSON.stringify(out, null, 2));
console.log("wrote", outPath);
