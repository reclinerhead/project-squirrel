import { describe, expect, it } from "vitest";
import { frameFilename, frameUrl } from "./frames";

describe("frameFilename", () => {
  it("maps a frame id to the names the archiver writes", () => {
    const fid = "20260714_081500_20260714T081530_arrival_0007";
    expect(frameFilename(fid, false)).toBe(`${fid}.jpg`);
    expect(frameFilename(fid, true)).toBe(`${fid}.thumb.jpg`);
  });
  it("rejects anything a filesystem could interpret (the traversal guard)", () => {
    for (const hostile of [
      "..",
      "../secrets",
      "..\\secrets",
      "a/b",
      "a.b", // dots never appear in minted ids
      "%2e%2e",
      "id?x=1",
      "",
      " ",
    ]) {
      expect(frameFilename(hostile, false)).toBeNull();
      expect(frameFilename(hostile, true)).toBeNull();
    }
  });
});

describe("frameUrl", () => {
  it("serves full-size bare and the thumbnail via ?thumb=1", () => {
    expect(frameUrl("fid_1")).toBe("/frames/fid_1");
    expect(frameUrl("fid_1", true)).toBe("/frames/fid_1?thumb=1");
  });
});
