import { describe, expect, it } from "vitest";
import { formatDuration, formatKhz, formatTotalDuration } from "./format";

describe("formatDuration", () => {
  it("renders m:ss with zero-padded seconds", () => {
    expect(formatDuration(227)).toBe("3:47");
    expect(formatDuration(60)).toBe("1:00");
    expect(formatDuration(61)).toBe("1:01");
    expect(formatDuration(9)).toBe("0:09");
    expect(formatDuration(0)).toBe("0:00");
  });

  it("adds an hours part only past 60 minutes, padding minutes then", () => {
    expect(formatDuration(3600)).toBe("1:00:00");
    expect(formatDuration(4245)).toBe("1:10:45");
    expect(formatDuration(3599)).toBe("59:59");
  });

  it("floors fractional seconds and clamps negatives to zero", () => {
    expect(formatDuration(90.9)).toBe("1:30");
    expect(formatDuration(-5)).toBe("0:00");
  });
});

describe("formatTotalDuration", () => {
  it("stays in minutes under an hour", () => {
    expect(formatTotalDuration(2729)).toBe("45 min");
    expect(formatTotalDuration(60)).toBe("1 min");
  });

  it("switches to hr + min at an hour, dropping a zero-minute part", () => {
    expect(formatTotalDuration(5520)).toBe("1 hr 32 min");
    expect(formatTotalDuration(7200)).toBe("2 hr");
  });

  it("rounds to the nearest minute", () => {
    expect(formatTotalDuration(89)).toBe("1 min");
    expect(formatTotalDuration(91)).toBe("2 min");
  });
});

describe("formatKhz", () => {
  it("drops the decimal for whole kHz and keeps one digit otherwise", () => {
    expect(formatKhz(48000)).toBe("48");
    expect(formatKhz(96000)).toBe("96");
    expect(formatKhz(44100)).toBe("44.1");
    expect(formatKhz(88200)).toBe("88.2");
  });
});
