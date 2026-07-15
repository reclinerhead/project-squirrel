// The fixture library (issue #116). Every shape mirrors the Phase 0 catalog
// so swapping in real data is a change to lib/api.ts, not to anything above
// it. The bands are invented -- this is placeholder data, not a real library
// -- but the format mix deliberately echoes the real one (epic #115): mostly
// ALAC, a healthy MP3 minority, a little FLAC, so every quality-badge tier
// shows up somewhere in the UI.

import type { Album, Artist, AudioFormat, Track } from "./types";

// --- compact builders (fixture-only, not exported) ---

function secs(mss: string): number {
  const [m, s] = mss.split(":");
  return Number(m) * 60 + Number(s);
}

type FormatSpec = {
  format: AudioFormat;
  bitDepth: number | null;
  sampleRateHz: number | null;
  bitrateKbps: number | null;
};

const alac2448: FormatSpec = { format: "alac", bitDepth: 24, sampleRateHz: 48000, bitrateKbps: null };
const alac2496: FormatSpec = { format: "alac", bitDepth: 24, sampleRateHz: 96000, bitrateKbps: null };
const alac1644: FormatSpec = { format: "alac", bitDepth: 16, sampleRateHz: 44100, bitrateKbps: null };
const flac2448: FormatSpec = { format: "flac", bitDepth: 24, sampleRateHz: 48000, bitrateKbps: null };
const flac1644: FormatSpec = { format: "flac", bitDepth: 16, sampleRateHz: 44100, bitrateKbps: null };
const mp3320: FormatSpec = { format: "mp3", bitDepth: null, sampleRateHz: 44100, bitrateKbps: 320 };
const mp3256: FormatSpec = { format: "mp3", bitDepth: null, sampleRateHz: 44100, bitrateKbps: 256 };

function album(
  artistId: string,
  artist: string,
  id: string,
  title: string,
  year: number,
  spec: FormatSpec,
  tracks: [title: string, mss: string][],
): Album {
  return {
    id,
    title,
    artistId,
    artist,
    year,
    tracks: tracks.map(([t, d], i): Track => ({
      id: `${id}-t${i + 1}`,
      title: t,
      artistId,
      artist,
      albumId: id,
      album: title,
      trackNo: i + 1,
      durationS: secs(d),
      ...spec,
    })),
  };
}

// --- the library ---

export const ARTISTS: Artist[] = [
  {
    id: "driveway-ghosts",
    name: "Driveway Ghosts",
    bio:
      "Driveway Ghosts formed in a detached garage outside Traverse City, where the only audience for their first two winters was whatever the motion light caught crossing the gravel. Their sound splits the difference between heartland rock and something colder and more patient -- brushed drums, baritone guitar, harmonies that arrive a half-beat late like they walked over from the neighbor's place. The band self-recorded both of their albums to a salvaged 8-track console, and it shows in the best way: everything sounds one room away. Critics keep reaching for the word 'nocturnal', and the band keeps not correcting them. They tour rarely, in fall, and are rumored to soundcheck with the house lights off.",
    albums: [
      album("driveway-ghosts", "Driveway Ghosts", "gravel-static", "Gravel Static", 2023, alac2448, [
        ["Motion Light", "4:12"],
        ["Second Winter", "3:48"],
        ["Salt on the Steps", "5:02"],
        ["Cul-de-sac Choir", "3:31"],
        ["Half-Beat Late", "4:44"],
        ["The Neighbor's Radio", "3:19"],
        ["Frost Heave", "6:07"],
        ["Porchlight Economy", "4:26"],
        ["Gravel Static", "5:38"],
      ]),
      album("driveway-ghosts", "Driveway Ghosts", "north-of-the-thaw", "North of the Thaw", 2025, alac2448, [
        ["Thaw Line", "4:05"],
        ["Bare Argument", "3:57"],
        ["Wool and Wire", "4:40"],
        ["Last Plow Out", "3:22"],
        ["Copper Sky", "5:15"],
        ["Every Yard a Field", "4:11"],
        ["Sleeping Porch", "6:24"],
        ["North of the Thaw", "5:49"],
      ]),
    ],
  },
  {
    id: "signal-creek",
    name: "Signal Creek",
    bio:
      "Signal Creek is the solo vehicle of producer Ana Reyes, who records synthesizers through spring reverbs in a cabin with famously bad cell coverage -- the project's name is the spot at the end of the drive where one bar appears. Her tracks build like weather systems: a pulse, then layers, then all of it at once.",
    albums: [
      album("signal-creek", "Signal Creek", "one-bar", "One Bar", 2024, alac2496, [
        ["Coverage Map", "5:21"],
        ["Dropped Call", "4:14"],
        ["Repeater", "6:03"],
        ["End of the Drive", "4:52"],
        ["Weather System", "7:16"],
        ["Spring Reverb", "3:58"],
        ["One Bar", "5:44"],
      ]),
      album("signal-creek", "Signal Creek", "night-air", "Night Air", 2022, alac1644, [
        ["Antenna Farm", "4:31"],
        ["AM Ghost", "5:09"],
        ["Skywave", "6:18"],
        ["Carrier Tone", "3:47"],
        ["Ionosphere", "5:55"],
        ["Clear Channel", "4:23"],
        ["Night Air", "7:02"],
      ]),
    ],
  },
  {
    id: "the-cold-frame",
    name: "The Cold Frame",
    bio:
      "Instrumental post-rock quartet from Duluth. The Cold Frame write ten-minute pieces the way gardeners overwinter seedlings -- slowly, under glass, with total confidence the sun is coming. Two guitars, a cello, and a drummer who plays like he's paid by the silence.",
    albums: [
      album("the-cold-frame", "The Cold Frame", "under-glass", "Under Glass", 2021, flac1644, [
        ["Germination", "8:44"],
        ["Hardening Off", "6:32"],
        ["First True Leaves", "9:17"],
        ["Under Glass", "11:05"],
        ["Transplant Shock", "7:23"],
      ]),
    ],
  },
  {
    id: "maple-and-vine",
    name: "Maple & Vine",
    bio:
      "Maple & Vine are a folk duo -- one voice like cedar, one like smoke -- who met trading verses at a farmers-market open mic and never quite stopped. Their songs are short, sturdy, and built to be hummed while doing something else with your hands.",
    albums: [
      album("maple-and-vine", "Maple & Vine", "handmade-weather", "Handmade Weather", 2020, mp3320, [
        ["Jar by the Door", "2:58"],
        ["Cedar and Smoke", "3:24"],
        ["Market Day", "2:41"],
        ["Two Chairs", "3:52"],
        ["Little Engine", "2:33"],
        ["Handmade Weather", "4:08"],
        ["Clothesline Semaphore", "3:11"],
        ["Last Stall on the Left", "3:37"],
      ]),
      album("maple-and-vine", "Maple & Vine", "the-long-table", "The Long Table", 2023, mp3320, [
        ["Set an Extra Place", "3:19"],
        ["Bread and Argument", "2:54"],
        ["The Long Table", "4:21"],
        ["Preserves", "3:03"],
        ["Company Coming", "2:47"],
        ["Sweep the Porch", "3:33"],
        ["Candle Math", "3:58"],
      ]),
    ],
  },
  {
    id: "low-antler",
    name: "Low Antler",
    bio:
      "Slowcore three-piece from the Upper Peninsula. Low Antler play at the speed of snowfall and mix their records so quiet passages make you lean in -- then reward the lean. Their lone album took four winters to finish and sounds like all of them.",
    albums: [
      album("low-antler", "Low Antler", "four-winters", "Four Winters", 2024, flac2448, [
        ["Shed Season", "6:41"],
        ["Browse Line", "7:28"],
        ["Yarding Up", "5:56"],
        ["Crust Walker", "8:13"],
        ["Four Winters", "9:34"],
        ["Velvet, Later", "6:02"],
      ]),
    ],
  },
  {
    id: "night-shift-owls",
    name: "The Night Shift Owls",
    bio:
      "A jazz quartet that only books the late set. The Night Shift Owls swing hard but talk soft -- brushed kit, upright bass, a trumpet with a practice mute, and a pianist who quotes lullabies when she thinks no one's listening.",
    albums: [
      album("night-shift-owls", "The Night Shift Owls", "last-set", "Last Set", 2019, alac1644, [
        ["Doors at Eleven", "5:47"],
        ["Brushes Only", "4:29"],
        ["Mute Point", "6:15"],
        ["Lullaby Quote", "5:03"],
        ["Closing Time Waltz", "7:21"],
        ["Tip Jar Blues", "4:44"],
        ["Last Set", "8:09"],
      ]),
      album("night-shift-owls", "The Night Shift Owls", "early-birds", "Early Birds", 2022, alac1644, [
        ["First Light Fake", "5:12"],
        ["Alarm Snooze", "4:37"],
        ["Percolator", "3:58"],
        ["Sunrise, Reluctantly", "6:26"],
        ["Day Sleeper", "5:31"],
        ["Early Birds", "7:14"],
      ]),
    ],
  },
  {
    id: "carbide-lamp",
    name: "Carbide Lamp",
    bio:
      "Greasy two-man blues from an iron town -- one resonator guitar, one kit assembled partly from actual mining equipment. Carbide Lamp songs are about water in the shaft, money owed, and headlamps that never quite die.",
    albums: [
      album("carbide-lamp", "Carbide Lamp", "seam-and-vein", "Seam and Vein", 2018, mp3256, [
        ["Down the Ladder Road", "3:44"],
        ["Water in the Shaft", "4:17"],
        ["Company Scrip", "3:29"],
        ["Seam and Vein", "5:08"],
        ["Headlamp Never Dies", "4:36"],
        ["Tailings", "3:51"],
        ["Cage Call", "4:02"],
      ]),
    ],
  },
  {
    id: "painted-bunting",
    name: "Painted Bunting",
    bio:
      "Dream pop with field recordings stitched through it -- every Painted Bunting track hides at least one real bird, and the liner notes credit them by species. The band swears the title track's rhythm section is two woodpeckers and a screen door.",
    albums: [
      album("painted-bunting", "Painted Bunting", "plumage", "Plumage", 2025, alac2448, [
        ["Molt", "4:18"],
        ["Seven Colors", "3:52"],
        ["Feeder Politics", "4:41"],
        ["Screen Door Percussion", "3:26"],
        ["Nest Architecture", "5:14"],
        ["Plumage", "5:57"],
        ["Fledge", "4:03"],
      ]),
    ],
  },
  {
    id: "freight-elevator",
    name: "Freight Elevator",
    bio:
      "Instrumental beats made in an actual freight elevator, which the producer insists has the best natural compression in the building. Dusty loops, thick bass, doors that open exactly on the one.",
    albums: [
      album("freight-elevator", "Freight Elevator", "service-entrance", "Service Entrance", 2021, mp3320, [
        ["Hold the Door", "2:47"],
        ["Manifest", "3:12"],
        ["Between Floors", "2:58"],
        ["Counterweight", "3:41"],
        ["Service Entrance", "3:07"],
        ["Loading Dock", "2:39"],
        ["Overhead Clearance", "3:24"],
        ["Call Button", "2:51"],
      ]),
    ],
  },
  {
    id: "quiet-meadow-society",
    name: "Quiet Meadow Society",
    bio:
      "Chamber pop collective -- strings, woodwinds, and a rotating cast of singers who all sound like they're apologizing for waking you. The Society records live to two microphones in a decommissioned grange hall.",
    albums: [
      album("quiet-meadow-society", "Quiet Meadow Society", "grange-hall", "Grange Hall", 2023, flac1644, [
        ["Meeting Called to Order", "3:36"],
        ["Minutes of the Last Meadow", "4:22"],
        ["Motion to Adjourn", "3:14"],
        ["Two Microphones", "5:01"],
        ["Grange Hall", "4:48"],
        ["All in Favor", "3:29"],
        ["Dues", "4:15"],
      ]),
    ],
  },
];

// Fixture-curated top tracks per artist. In the real system this ranking
// comes from play_history (epic #115) -- which is exactly why it is NOT a
// field on Artist: the catalog doesn't own it, listening does.
export const TOP_TRACKS: Record<string, string[]> = {
  "driveway-ghosts": ["gravel-static-t1", "north-of-the-thaw-t5", "gravel-static-t7", "north-of-the-thaw-t1", "gravel-static-t9"],
  "signal-creek": ["one-bar-t5", "night-air-t3", "one-bar-t1", "night-air-t6"],
  "the-cold-frame": ["under-glass-t4", "under-glass-t1", "under-glass-t3"],
  "maple-and-vine": ["handmade-weather-t2", "the-long-table-t3", "handmade-weather-t6", "the-long-table-t1"],
  "low-antler": ["four-winters-t5", "four-winters-t1", "four-winters-t4"],
  "night-shift-owls": ["last-set-t7", "early-birds-t4", "last-set-t3", "early-birds-t1"],
  "carbide-lamp": ["seam-and-vein-t5", "seam-and-vein-t2", "seam-and-vein-t4"],
  "painted-bunting": ["plumage-t6", "plumage-t2", "plumage-t4"],
  "freight-elevator": ["service-entrance-t4", "service-entrance-t1", "service-entrance-t5"],
  "quiet-meadow-society": ["grange-hall-t4", "grange-hall-t5", "grange-hall-t2"],
};
