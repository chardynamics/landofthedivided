#!/usr/bin/env python3
import argparse
import csv
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOCALISATION_FILE = ROOT / "localisation" / "english" / "state_names_l_english.yml"
STATE_DIR = ROOT / "history" / "states"
OUTPUT_CSV = ROOT / "state_population_estimates.csv"

EXACT_POPULATIONS = {
    # US boroughs / major cities (NYC)
    "manhattan, nyc": 1537195,
    "queens, ny": 2229379,
    "brooklyn, ny": 2465326,
    "the bronx, ny": 1332650,
    "bronx, ny": 1332650,
    "staten island, ny": 443728,
    
    # Major metro areas
    "chicago, il": 2896016,
    "los angeles, ca": 3694820,
    "san francisco, ca": 776733,
    "philadelphia, pa": 1517550,
    "houston, tx": 1953631,
    "san antonio, tx": 1144646,
    "dallas, tx": 1188580,
    "austin, tx": 656562,
    "denver, co": 554636,
    "phoenix, az": 1321045,
    "san diego, ca": 1223400,
    "san jose, ca": 894943,
    "minneapolis, mn": 382618,
    "detroit, mi": 951270,
    "tucson, az": 486699,
    "columbus, oh": 711470,
    "jacksonville, fl": 735617,
    "new orleans, la": 484674,
    "seattle, wa": 563374,
    "portland, or": 529121,
    "cincinnati, oh": 331285,
    "atlanta, ga": 416474,
    "baltimore, md": 651154,
    "miami, fl": 362470,
    "orlando, fl": 185951,
    "sacramento, ca": 407018,
    "long beach, ca": 461522,
    "oakland, ca": 399484,
    "fresno, ca": 427652,
    "fort worth, tx": 534694,
    "el paso, tx": 563662,
    "arlington, tx": 332969,
    "corpus christi, tx": 277454,
    "oklahoma city, ok": 506132,
    "tulsa, ok": 393049,
    "buffalo, ny": 292648,
    "rochester, ny": 219773,
    "new york, ny": 8008278,
    "cleveland, oh": 478403,
    "raleigh, nc": 276093,
    "charlotte, nc": 540828,
    "greensboro, nc": 223891,
    "durham, nc": 187035,
    "wilmington, nc": 75838,
    "nashville, tn": 569891,
    "memphis, tn": 650100,
    "chattanooga, tn": 155554,
    "knoxville, tn": 173890,
    "louisville, ky": 256231,
    "lexington, ky": 260512,
    "kansas city, mo": 441545,
    "st. louis, mo": 347189,
    "springfield, mo": 151580,
    "st. joseph, mo": 73990,
    "joplin, mo": 45504,
    "topeka, ks": 119883,
    "wichita, ks": 344284,
    "kansas city, ks": 146866,
    "omaha, ne": 390007,
    "lincoln, ne": 225581,
    "des moines, ia": 198682,
    "cedar rapids, ia": 120758,
    "davenport, ia": 98359,
    "waterloo, ia": 68747,
    "iowa city, ia": 62220,
    "council bluffs, ia": 58268,
    "dubuque, ia": 57546,
    "sioux city, ia": 85013,
    "billings, mt": 89847,
    "bozeman, mt": 27549,
    "missoula, mt": 63891,
    "great falls, mt": 56121,
    "butte, mt": 32224,
    "helena, mt": 25780,
    "kalispell, mt": 14223,
    "boise, id": 185787,
    "pocatello, id": 51466,
    "idaho falls, id": 50730,
    "nampa, id": 51867,
    "coeur d'alene, id": 34514,
    "moscow, id": 23391,
    "jerome, id": 7780,
    "salmon, id": 3122,
    "jerome, id": 7780,
    "robinson, id": 50,
    
    # Upper Midwest counties
    "minnetonka, mn": 51472,
    "bloomington, mn": 85172,
    "rochester, mn": 85806,
    "duluth, mn": 86918,
    "brainerd, mn": 13604,
    "mankato, mn": 32427,
    "hutchinson, mn": 14178,
    "marshall, mn": 13680,
    "willmar, mn": 19305,
    "marshall, mn": 13680,
    "murrayl, mn": 36000,
    "bemidji, mn": 13802,
    
    # Wisconsin cities
    "milwaukee, wi": 596974,
    "madison, wi": 208054,
    "green bay, wi": 102313,
    "kenosha, wi": 90352,
    "racine, wi": 81855,
    "appleton, wi": 70087,
    "waukesha, wi": 64825,
    "eau claire, wi": 61704,
    "oshkosh, wi": 62966,
    "west allis, wi": 61254,
    "wauwatosa, wi": 47717,
    "sheboygan, wi": 50792,
    "la crosse, wi": 51520,
    "green bay, wi": 102313,
    "stevens point, wi": 24551,
    
    # Michigan cities
    "grand rapids, mi": 197800,
    "warren, mi": 138247,
    "flint, mi": 124943,
    "sterling heights, mi": 124471,
    "ann arbor, mi": 114024,
    "livonia, mi": 100545,
    "dearborn, mi": 97775,
    "westland, mi": 86602,
    "taylor, mi": 63131,
    "pontiac, mi": 66337,
    "kalamazoo, mi": 77145,
    "saginaw, mi": 61799,
    "lansing, mi": 119128,
    "marquette, mi": 21297,
    "petoskey, mi": 6080,
    "alpena, mi": 31314,
    "cadillac, mi": 10998,
    "benton harbor, mi": 12818,
    
    # Ohio cities & counties
    "cleveland, oh": 478403,
    "cincinnati, oh": 331285,
    "toledo, oh": 313619,
    "dayton, oh": 166179,
    "akron, oh": 217074,
    "columbus, oh": 711470,
    "parma, oh": 84272,
    "canton, oh": 80806,
    "youngstown, oh": 82026,
    "athens, oh": 21342,
    "richland, oh": 128852,
    "sandusky, oh": 27844,
    "williams, oh": 36956,
    "clinton, oh": 4253,
    
    # Indiana cities
    "indianapolis, in": 781870,
    "fort wayne, in": 205727,
    "evansville, in": 121582,
    "gary, in": 102746,
    "south bend, in": 107789,
    "bloomington, in": 69291,
    "muncie, in": 67430,
    "anderson, in": 59734,
    "hammond, in": 83048,
    "terre haute, in": 59614,
    "lafayette, in": 56397,
    "bartholomew, in": 71968,
    "delaware, in": 118769,
    "floyd, in": 74582,
    "howard, in": 84964,
    "jasper, in": 28602,
    
    # Illinois cities & counties  
    "chicago, il": 2896016,
    "rockford, il": 150115,
    "aurora, il": 142990,
    "peoria, il": 112936,
    "springfield, il": 111454,
    "bloomington, il": 64344,
    "champaign, il": 67518,
    "naperville, il": 128358,
    "danville, il": 33904,
    "belleville, il": 42141,
    "kankakee, il": 27409,
    "petersburg, il": 2250,
    "chicago metro, il": 9512999,
    "knox, il": 55836,
    "adams, il": 68277,
    
    # Missouri cities & counties
    "kansas city, mo": 441545,
    "st. louis, mo": 347189,
    "springfield, mo": 151580,
    "independence, mo": 113288,
    "st. charles, mo": 60321,
    "columbia, mo": 84465,
    "joplin, mo": 45504,
    "st. joseph, mo": 73990,
    "jefferson city, mo": 39636,
    "poplar bluff, mo": 17023,
    "harrisonville, mo": 9719,
    "bowling green, mo": 5836,
    "gasconade, mo": 15342,
    "shannon, mo": 8324,
    "north kansas city, mo": 4384,
    "adalair, mo": 1800,
    
    # Arkansas cities & counties
    "little rock, ar": 183133,
    "fayetteville, ar": 58047,
    "fort smith, ar": 80268,
    "jonesboro, ar": 55515,
    "pine bluff, ar": 55085,
    "conway, ar": 43167,
    "hot springs, ar": 32462,
    "el dorado, ar": 23646,
    "phillips, ar": 26445,
    "little river, ar": 13628,
    "de witt, ar": 3647,
    
    # Louisiana cities & counties
    "new orleans, la": 484674,
    "baton rouge, la": 227818,
    "shreveport, la": 200145,
    "lafayette, la": 127141,
    "monroe, la": 54560,
    "alexandria, la": 46342,
    "lake charles, la": 71757,
    "houma, la": 32393,
    "natchitoches, la": 4276,
    "la salle, la": 14282,
    "lafourche, la": 89974,
    "breton sound islands, la": 0,
    
    # Texas cities & counties
    "houston, tx": 1953631,
    "dallas, tx": 1188580,
    "austin, tx": 656562,
    "san antonio, tx": 1144646,
    "fort worth, tx": 534694,
    "el paso, tx": 563662,
    "arlington, tx": 332969,
    "corpus christi, tx": 277454,
    "plano, tx": 222030,
    "garland, tx": 215768,
    "irving, tx": 191615,
    "laredo, tx": 176576,
    "lubbock, tx": 199564,
    "amarillo, tx": 173627,
    "beaumont, tx": 113866,
    "waco, tx": 113007,
    "mcallen, tx": 106414,
    "abilene, tx": 115930,
    "brownsville, tx": 139722,
    "odessa, tx": 90943,
    "harlingen, tx": 64849,
    "tyler, tx": 83650,
    "longview, tx": 80455,
    "dumas, tx": 14229,
    "crockett, tx": 7141,
    "fredericksburg, tx": 10530,
    "bastrop, tx": 8195,
    "greater dallas-fort worth area, tx": 5249420,
    "clear lake, tx": 84577,
    
    # Oklahoma cities
    "oklahoma city, ok": 506132,
    "tulsa, ok": 393049,
    "norman, ok": 95694,
    "broken arrow, ok": 74859,
    "edmond, ok": 68315,
    "lawton, ok": 92757,
    "enid, ok": 46040,
    "stillwater, ok": 39065,
    "ada, ok": 16008,
    "mcalester, ok": 18242,
    "woodward, ok": 12051,
    
    # Kansas cities
    "kansas city, ks": 146866,
    "wichita, ks": 344284,
    "topeka, ks": 119883,
    "olathe, ks": 92962,
    "dodge city, ks": 25176,
    "lawrence, ks": 80098,
    "salina, ks": 45966,
    "emporia, ks": 26760,
    "liberal, ks": 19666,
    "overland park, ks": 173372,
    "cowley, ks": 36291,
    "ellis, ks": 27507,
    "saint francis, ks": 1277,
    
    # Nebraska cities & counties
    "omaha, ne": 390007,
    "lincoln, ne": 225581,
    "bellevue, ne": 44382,
    "grand island, ne": 42862,
    "kearney, ne": 27431,
    "fremont, ne": 25174,
    "north platte, ne": 23378,
    "hastings, ne": 24064,
    "adams, ne": 31364,
    "beatrice, ne": 12522,
    "cherry, ne": 5713,
    "furnass, ne": 3329,
    "ogallala, ne": 4726,
    
    # South Dakota cities & counties
    "sioux falls, sd": 123975,
    "rapid city, sd": 54523,
    "pierre, sd": 13876,
    "brookings, sd": 18504,
    "mitchell, sd": 15254,
    "watertown, sd": 21482,
    "yankton, sd": 13528,
    "huron, sd": 11893,
    "aberdeen, sd": 24658,
    "mobridge, sd": 3637,
    "harding, sd": 1353,
    "trippe, sd": 500,
    
    # North Dakota cities
    "bismarck, nd": 55532,
    "fargo, nd": 74111,
    "grand forks, nd": 49747,
    "minot, nd": 36567,
    "williston, nd": 12512,
    "dickinson, nd": 16010,
    "jamestown, nd": 15427,
    "devils lake, nd": 7084,
    "mercer, nd": 2640,
    
    # Wyoming cities
    "cheyenne, wy": 81868,
    "casper, wy": 49644,
    "laramie, wy": 30405,
    "gillette, wy": 19646,
    "rock springs, wy": 19050,
    "sheridan, wy": 17244,
    "jackson, wy": 8647,
    "cody, wy": 9520,
    "evanston, wy": 12359,
    "east superior, wy": 3500,
    "douglas, wy": 5288,
    "pine bluffs, wy": 1926,
    "adobie town rim, wy": 150,
    
    # Colorado cities & counties
    "denver, co": 554636,
    "colorado springs, co": 360890,
    "aurora, co": 222103,
    "fort collins, co": 118652,
    "pueblo, co": 102121,
    "boulder, co": 94673,
    "westminster, co": 100940,
    "lakewood, co": 144126,
    "thornton, co": 82384,
    "loveland, co": 50608,
    "greeley, co": 76930,
    "canon city, co": 16400,
    "durango, co": 12675,
    "fort morgan, co": 8042,
    "grand junction, co": 41986,
    "aspen, co": 3991,
    "breckenridge, co": 1481,
    "dove creek, co": 633,
    "alamosa, co": 8780,
    "kit carson, co": 8011,
    "delta, co": 6033,
    
    # New Mexico cities & counties
    "albuquerque, nm": 448607,
    "las cruces, nm": 74267,
    "santa fe, nm": 62203,
    "rio rancho, nm": 51765,
    "roswell, nm": 45293,
    "clovis, nm": 32667,
    "farmington, nm": 37844,
    "deming, nm": 14116,
    "carlsbad, nm": 25625,
    "cibola, nm": 27213,
    "clayton, nm": 3175,
    "chaves, nm": 61382,
    "curry, nm": 46389,
    
    # Arizona cities & counties
    "phoenix, az": 1321045,
    "mesa, az": 396375,
    "chandler, az": 176581,
    "glendale, az": 218812,
    "scottsdale, az": 202705,
    "gilbert, az": 109697,
    "tempe, az": 158625,
    "peoria, az": 108364,
    "tucson, az": 486699,
    "flagstaff, az": 65112,
    "prescott, az": 33938,
    "sierra vista, az": 43888,
    "san carlos, az": 2719,
    "kaibab, az": 11049,
    "pima, az": 1247,
    "duncan, az": 4283,
    
    # California cities & counties
    "los angeles, ca": 3694820,
    "san francisco, ca": 776733,
    "san diego, ca": 1223400,
    "san jose, ca": 894943,
    "fresno, ca": 427652,
    "sacramento, ca": 407018,
    "long beach, ca": 461522,
    "oakland, ca": 399484,
    "bakersfield, ca": 247057,
    "stockton, ca": 243771,
    "riverside, ca": 255166,
    "anaheim, ca": 328014,
    "santa ana, ca": 337977,
    "irvine, ca": 143072,
    "chico, ca": 59954,
    "redding, ca": 80865,
    "alturas, ca": 10522,
    "bishop, ca": 3574,
    "crescent city, ca": 4006,
    "el centro, ca": 31384,
    "mojave, ca": 6483,
    "napa, ca": 72585,
    
    # Oregon cities
    "portland, or": 529121,
    "eugene, or": 137893,
    "salem, or": 136924,
    "gresham, or": 68235,
    "hillsboro, or": 70186,
    "beaverton, or": 76129,
    "medford, or": 63299,
    "springfield, or": 52864,
    "bend, or": 52029,
    "klamath falls, or": 19462,
    "ontario, or": 10365,
    "pendleton, or": 16354,
    "roseburg, or": 20017,
    "fremont-winema, or": 3000,
    
    # Washington cities
    "seattle, wa": 563374,
    "tacoma, wa": 193556,
    "vancouver, wa": 143560,
    "bellevue, wa": 109569,
    "kent, wa": 79524,
    "everett, wa": 91488,
    "renton, wa": 50052,
    "federal way, wa": 83259,
    "spokane, wa": 195629,
    "olympia, wa": 42514,
    "bellingham, wa": 67171,
    "yakima, wa": 71845,
    "pullman, wa": 24844,
    "walla walla, wa": 29686,
    "columbia basin, wa": 110000,
    "eastern okanogan highlands, wa": 35000,
    "metaline falls, wa": 252,
    "naval submarine base bangor, wa": 1500,
    "north puget sound, wa": 120000,
    "northern cascades, wa": 75000,
    "olympic peninsula, wa": 70000,
    "puget islands, wa": 25000,
    "south puget sound, wa": 100000,
    "southern cascades, wa": 65000,
    "snoqualmie pass, wa": 1000,
    
    # Idaho continued
    "moscow, id": 23391,
    "idaho falls, id": 50730,
    "pocatello, id": 51466,
    "nampa, id": 51867,
    "boise, id": 185787,
    "coeur d'alene, id": 34514,
    "jerome, id": 7780,
    "cascade, id": 1100,
    "roberts, id": 599,
    "trinity, id": 50,
    "salmon, id": 3122,
    
    # Montana continued
    "billings, mt": 89847,
    "missoula, mt": 63891,
    "butte, mt": 32224,
    "great falls, mt": 56121,
    "bozeman, mt": 27549,
    "helena, mt": 25780,
    "kalispell, mt": 14223,
    "browning, mt": 1016,
    "baker, mt": 1772,
    "dillon, mt": 3796,
    "east bozeman, mt": 25000,
    "hardin, mt": 860,
    "madison, mt": 2000,
    "ronan, mt": 1915,
    
    # Utah cities
    "salt lake city, ut": 181898,
    "west valley city, ut": 108896,
    "provo, ut": 105166,
    "sandy, ut": 88418,
    "orem, ut": 84324,
    "ogden, ut": 77226,
    "west jordan, ut": 68336,
    "layton, ut": 58336,
    "lehi, ut": 47407,
    "cedar city, ut": 20527,
    "moab, ut": 5333,
    "mexican hat, ut": 52,
    "delta, ut": 3342,
    "vernal, ut": 7714,
    "capitol reef, ut": 0,
    "randell, ut": 0,
    
    # Nevada cities
    "las vegas, nv": 478434,
    "henderson, nv": 175381,
    "reno, nv": 180480,
    "carson city, nv": 52457,
    "mesquite, nv": 1873,
    "elko, nv": 17269,
    "ely, nv": 4041,
    "lovelock, nv": 2157,
    "tonopah, nv": 2478,
    "west wendover, nv": 2607,
    "mojave, nv": 800,
    
    # New Hampshire cities
    "manchester, nh": 107006,
    "nashua, nh": 86605,
    "concord, nh": 40687,
    "derry, nh": 34021,
    "rochester, nh": 28461,
    "salem, nh": 28112,
    "grafton, nh": 13233,
    "great north woods, nh": 40000,
    "hillsborough, nh": 44654,
    "merrimack, nh": 115794,
    "strafford, nh": 114230,
    
    # Vermont cities
    "burlington, vt": 39127,
    "rutland, vt": 17292,
    "barre, vt": 9052,
    "montpelier, vt": 8035,
    "brattleboro, vt": 12124,
    "addison, vt": 17630,
    "franklin, vt": 47746,
    "northeast kingdom, vt": 25000,
    "windham, vt": 43156,
    
    # Maine cities & counties
    "portland, me": 64249,
    "lewiston, me": 36592,
    "bangor, me": 31473,
    "auburn, me": 23033,
    "waterville, me": 15537,
    "aroostook, me": 91331,
    "downeast, me": 35000,
    "mahoosuc range, me": 20000,
    "penobscot bay, me": 45000,
    "piscataquis, me": 17235,
    "portland casco bay, me": 64249,
    "somerset, me": 50888,
    "west mahoosuc, me": 15000,
    
    # Massachusetts cities
    "boston, ma": 589141,
    "worcester, ma": 172648,
    "springfield, ma": 152319,
    "cambridge, ma": 101355,
    "lowell, ma": 103439,
    "brockton, ma": 94304,
    "new bedford, ma": 93768,
    "fall river, ma": 91938,
    "salem, ma": 40556,
    "barnstable, ma": 47821,
    "berkshire, ma": 139352,
    "essex, ma": 743159,
    "plymouth, ma": 51701,
    "suffolk, ma": 689039,
    "worcester, ma": 172648,
    
    # Connecticut cities
    "hartford, ct": 121578,
    "new haven, ct": 123626,
    "bridgeport, ct": 139529,
    "fairfield, ct": 57340,
    "new london, ct": 25862,
    "stamford, ct": 117083,
    "waterbury, ct": 107271,
    
    # Rhode Island cities
    "providence, ri": 173618,
    "warwick, ri": 85808,
    "cranston, ri": 79269,
    "pawtucket, ri": 72644,
    "woonsocket, ri": 43224,
    "newport, ri": 26475,
    
    # New York cities & counties
    "new york, ny": 8008278,
    "buffalo, ny": 292648,
    "rochester, ny": 219773,
    "yonkers, ny": 196086,
    "syracuse, ny": 147306,
    "albany, ny": 95658,
    "utica, ny": 60651,
    "newburgh, ny": 28866,
    "auburn, ny": 27687,
    "watertown, ny": 27807,
    "ithaca, ny": 29287,
    "plattsburgh, ny": 19107,
    "malone, ny": 6076,
    "cattaraugus, ny": 80456,
    "chautauqua, ny": 139750,
    "chenango, ny": 48329,
    "columbia, ny": 63096,
    "delaware, ny": 43070,
    "dutchess, ny": 280150,
    "genesse, ny": 60370,
    "hamilton, ny": 4272,
    "herkimer, ny": 64427,
    "jefferson, ny": 113378,
    "king george's, ny": 40000,
    "madison, ny": 70503,
    "monroe, ny": 735343,
    "nassau, ny": 1334544,
    "new york, ny": 8008278,
    "orange, ny": 341367,
    "oswego, ny": 122577,
    "otsego, ny": 61676,
    "putnam, ny": 95745,
    "rensselaer, ny": 152538,
    "rockland, ny": 286753,
    "saratoga, ny": 200635,
    "schenectady, ny": 65566,
    "schoharie, ny": 31582,
    "seneca, ny": 35251,
    "st. lawrence, ny": 111931,
    "steuben, ny": 98726,
    "suffolk, ny": 1453734,
    "sullivan, ny": 73966,
    "tioga, ny": 51784,
    "tompkins, ny": 96501,
    "ulster, ny": 177749,
    "warren, ny": 63096,
    "washington, ny": 60042,
    "wayne, ny": 93772,
    "westchester, ny": 923459,
    "wyoming, ny": 43971,
    "yates, ny": 24621,
    
    # New Jersey cities
    "newark, nj": 273546,
    "jersey city, nj": 223532,
    "paterson, nj": 149222,
    "elizabeth, nj": 120568,
    "trenton, nj": 85403,
    "atlantic city, nj": 40517,
    "camden, nj": 79318,
    "princeton, nj": 14203,
    "middlesex, nj": 750162,
    "monmouth, nj": 615301,
    "sussex, nj": 144850,
    
    # Pennsylvania cities & counties
    "philadelphia, pa": 1517550,
    "pittsburgh, pa": 334563,
    "allentown, pa": 106632,
    "erie, pa": 103717,
    "reading, pa": 81207,
    "scranton, pa": 76415,
    "bethlehem, pa": 76135,
    "altoona, pa": 49523,
    "harrisburg, pa": 48950,
    "lancaster, pa": 56348,
    "levittown, pa": 52983,
    "lancaster, pa": 56348,
    "blair, pa": 129144,
    "bradford, pa": 62761,
    "bucks, pa": 597635,
    "centre, pa": 135758,
    "lackawanna, pa": 210718,
    "lawrence, pa": 96246,
    "lehigh, pa": 312090,
    "luzerne, pa": 319250,
    "lycoming, pa": 120044,
    "monroe, pa": 138687,
    "northampton, pa": 247105,
    "pike, pa": 46302,
    "schuylkill, pa": 150336,
    "tioga, pa": 41693,
    "wayne, pa": 47722,
    "westmoreland, pa": 369993,
    "wyoming, pa": 9270,
    "york, pa": 381751,
    "cambria, pa": 143679,
    "greene, pa": 40672,
    "fayette, pa": 148644,
    "forest, pa": 4946,
    "sullivan, pa": 6556,
    "warren, pa": 45050,
    "crawford, pa": 90366,
    "columbia, pa": 64151,
    "montour, pa": 18236,
    "snyder, pa": 37047,
    "union, pa": 41697,
    "perry, pa": 43602,
    "juniata, pa": 20539,
    "mifflin, pa": 46560,
    "huntingdon, pa": 45586,
    "bedford, pa": 49984,
    "fulton, pa": 14800,
    
    # Delaware cities
    "dover, de": 32135,
    "wilmington, de": 72664,
    "newark, de": 28547,
    "kent, de": 110993,
    "newcastle, de": 441946,
    "sussex, de": 197145,
    
    # Maryland cities & counties
    "baltimore, md": 651154,
    "montgomery, md": 873341,
    "prince george's, md": 863411,
    "anne arundel, md": 489656,
    "howard, md": 287085,
    "washington, dc": 572059,
    "arlington, va": 189453,
    "alexandria, va": 128283,
    "alleghany, md": 24023,
    "carroll, md": 150897,
    "frederick, md": 195277,
    "harford, md": 218590,
    "queen anne's, md": 47541,
    "wicomico, md": 84644,
    "somerset, md": 24747,
    "dorchester, md": 30674,
    "talbot, md": 33812,
    "kent, md": 19197,
    "cecil, md": 94503,
    
    # Virginia cities & counties
    "virginia beach, va": 425257,
    "norfolk, va": 234403,
    "richmond, va": 197790,
    "arlington, va": 189453,
    "alexandria, va": 128283,
    "newport news, va": 180719,
    "hampton, va": 146437,
    "roanoke, va": 94119,
    "lynchburg, va": 65269,
    "blacksburg, va": 39573,
    "accomack, va": 38305,
    "albemarle, va": 79236,
    "danville, va": 48411,
    "fairfax, va": 969749,
    "frederick, va": 67060,
    "hampton roads, va": 1600000,
    "king george's, va": 16803,
    "lynchburg, va": 65269,
    "prince george, va": 35127,
    "prince william, va": 280813,
    "stafford, va": 128026,
    "vinton, va": 28042,
    "wise, va": 42367,
    
    # West Virginia cities & counties
    "charleston, wv": 53421,
    "huntington, wv": 51475,
    "wheeling, wv": 31419,
    "morgantown, wv": 28719,
    "weirton, wv": 19746,
    "clarksburg, wv": 26326,
    "fairmont, wv": 18904,
    "beckley, wv": 17614,
    "bluefield, wv": 10447,
    "lewisburg, wv": 3795,
    "berkeley, wv": 75905,
    "cabell, wv": 96784,
    "greenbrier, wv": 34693,
    "kanawha, wv": 200073,
    "mineral, wv": 27078,
    "monogalia, wv": 81866,
    "ohio, wv": 47427,
    "raleight, wv": 79220,
    "wood, wv": 87986,
    
    # North Carolina cities & counties
    "charlotte, nc": 540828,
    "raleigh, nc": 276093,
    "greensboro, nc": 223891,
    "durham, nc": 187035,
    "winston-salem, nc": 185776,
    "wilmington, nc": 75838,
    "high point, nc": 85839,
    "fayetteville, nc": 121015,
    "asheville, nc": 68889,
    "greenville, nc": 60476,
    "chapel hill, nc": 48715,
    "cary, nc": 94536,
    "rocky mount, nc": 55893,
    "gastonia, nc": 66277,
    "beaufort, nc": 44915,
    "buncombe, nc": 206357,
    "cumberland, nc": 302963,
    "gaston, nc": 190365,
    "guilford, nc": 421048,
    "haywood, nc": 54033,
    "mecklenberg, nc": 695454,
    "mount airy, nc": 8270,
    "nash, nc": 95945,
    "new hanover, nc": 160307,
    "orange, nc": 118227,
    "person, nc": 39464,
    "pitt, nc": 133798,
    "randolph, nc": 141752,
    "robeson, nc": 123339,
    "rowan, nc": 130340,
    "rutherford, nc": 67810,
    "stokes, nc": 44711,
    "surry, nc": 71759,
    "wayne, nc": 113329,
    "yadkin, nc": 36348,
    
    # South Carolina cities & counties
    "charleston, sc": 96650,
    "columbia, sc": 116278,
    "greenville, sc": 56002,
    "rock hill, sc": 49765,
    "myrtle beach, sc": 22759,
    "florence, sc": 29813,
    "spartanburg, sc": 39673,
    "beaufort, sc": 23454,
    "horry, sc": 144053,
    "richland, sc": 269014,
    "york, sc": 164614,
    
    # Georgia cities & counties
    "atlanta, ga": 416474,
    "columbus, ga": 186291,
    "augusta, ga": 195182,
    "savannah, ga": 131510,
    "athens, ga": 100266,
    "macon, ga": 97255,
    "chatham, ga": 230765,
    "columbia, ga": 156714,
    "hall, ga": 139277,
    "houston, ga": 96255,
    "lowndes, ga": 92115,
    "muscogee, ga": 186291,
    "thomas, ga": 42737,
    "whitfield, ga": 83525,
    "north metro atlanta, ga": 600000,
    "south metro atlanta, ga": 600000,
    
    # Florida cities & counties
    "miami, fl": 362470,
    "tampa, fl": 303447,
    "orlando, fl": 185951,
    "st. petersburg, fl": 248098,
    "hialeah, fl": 226493,
    "fort lauderdale, fl": 152397,
    "jacksonville, fl": 735617,
    "tallahassee, fl": 150624,
    "key west, fl": 25478,
    "daytona beach, fl": 64112,
    "alachua, fl": 7052,
    "escambia, fl": 294410,
    "hillsborough, fl": 998948,
    "lee, fl": 440888,
    "leon, fl": 239452,
    "marion, fl": 258916,
    "orange, fl": 896344,
    "sarasota, fl": 325957,
    "palm beach, fl": 1131184,
    "broward, fl": 1623018,
    "dade, fl": 2306870,
    "duval, fl": 672318,
    "polk, fl": 483924,
    "pinellas, fl": 921482,
    
    # Alabama cities & counties
    "auburn, al": 42987,
    "autauga, al": 43671,
    "birmingham, al": 242820,
    "butler, al": 20947,
    "citronelle, al": 3867,
    "clarke, al": 27928,
    "dothan, al": 59341,
    "etowah, al": 103459,
    "gulf shores, al": 5117,
    "huntsville, al": 158216,
    "madison, al": 79156,
    "montgomery, al": 201568,
    "mobile, al": 198015,
    
    # Mississippi cities & counties
    "jackson, ms": 184256,
    "gulfport, ms": 71127,
    "biloxi, ms": 50644,
    "hattiesburg, ms": 41882,
    "meridian, ms": 41028,
    "greenville, ms": 41633,
    "vicksburg, ms": 26407,
    "oxford, ms": 14410,
    "tupelo, ms": 34546,
    "adams, ms": 34340,
    "lowndes, ms": 61721,
    
    # Kentucky cities & counties
    "louisville, ky": 256231,
    "lexington, ky": 260512,
    "covington, ky": 43370,
    "bowling green, ky": 49296,
    "owensboro, ky": 53549,
    "newport, ky": 15273,
    "frankfort, ky": 27098,
    "paducah, ky": 26307,
    "hopkinsville, ky": 30089,
    "henderson, ky": 27373,
    "christian, ky": 72462,
    "floyd county, ky": 31406,
    "hardin, ky": 96656,
    "madison, ky": 70872,
    "mccracken, ky": 33141,
    "mercer, ky": 20817,
    "nelson, ky": 37477,
    
    # Tennessee cities & counties
    "memphis, tn": 650100,
    "nashville, tn": 569891,
    "knoxville, tn": 173890,
    "chattanooga, tn": 155554,
    "clarksville, tn": 103340,
    "murfreesboro, tn": 69253,
    "jackson, tn": 59643,
    "johnson city, tn": 55469,
    "kingsport, tn": 44905,
    "coffee, tn": 48014,
    "dickson, tn": 43156,
    "maury, tn": 61958,
    
    # Canadian provinces/regions
    "ontario, on": 11208991,
    "quebec, qc": 7155482,
    "british columbia, bc": 3924804,
    "alberta, ab": 2910396,
    "manitoba, mb": 1235314,
    "saskatchewan, sk": 990321,
    "nova scotia, ns": 908007,
    "new brunswick, nb": 729498,
    "prince edward island, pei": 135294,
    "newfoundland, nl": 512930,
    "newfoundland and labrador, nl": 512930,
    "northwest territories, nt": 42941,
    "yukon, yt": 28674,
    "nunavut, nu": 29329,
    "sweet grass, ab": 0,
    "moresby island, bc": 148,
    "cape breton island, ns": 147414,
    "cockburn island, on": 0,
    "manitoulin island, on": 12786,
    "st. joseph island, on": 3585,
    "frontier, sk": 0,
    
    # Mexican states (2000 census data)
    "chihuahua, mx": 3028314,
    "coahuila, mx": 2415020,
    "baja california, mx": 2632004,
    "mexico, mx": 13096686,
    "jalisco, mx": 6322002,
    "veracruz, mx": 6228239,
    "guanajuato, mx": 4656761,
    "puebla, mx": 5076686,
    "mexico city, mx": 8591309,
    "monterrey, mx": 1108499,
    "guadalajara, mx": 3995392,
    "tampico, mx": 309546,
    "nuevo leon, mx": 3887133,
    "tamalipas, mx": 2413402,
    "baja california sur, mx": 512091,
    "yucatan, mx": 1685228,
    "sonora, mx": 2173302,
    "durango, mx": 1632934,
    "nayarit, mx": 920185,
    "sinaloa, mx": 2536844,
    "campeche, mx": 754730,
    "quintana roo, mx": 872569,
    "tabasco, mx": 1891829,
    "chiapas, mx": 3920892,
    "hidalgo, mx": 2235591,
    "queretaro, mx": 1404306,
    "tlaxcala, mx": 962646,
    "morelos, mx": 1555296,
    "guerrero, mx": 3079649,
    "oaxaca, mx": 3438765,
    "zacatecas, mx": 1353610,
    "aguascalientes, mx": 944285,
    "coahuila, mx": 2415020,
    "san luis potosi, mx": 2299684,
    "lopez mateos, mx": 35000,
    "tenochtitlan, mx": 8591309,
    "mexico city, mx": 8591309,
    "toluca, mx": 487612,
    "ecatepec, mx": 1547253,
    "naucalpan, mx": 830046,
    "tlanepantla, mx": 566312,
    "tlalnepantla, mx": 566312,
    "cuautitlan, mx": 450577,
    "iztapalapa, mx": 1815786,
    "gustavo a. madero, mx": 1257524,
    "miguel hidalgo, mx": 334933,
    "benito juarez, mx": 385439,
    "cuauhtemoc, mx": 526779,
    "venustiano carranza, mx": 430978,
    
    # Caribbean territories & special
    "puerto rico, pr": 3808610,
    "bahamas, bs": 303658,
    "havana, cu": 2102000,
    "turks and caicos, tc": 21240,
    "british virgin islands, bvi": 20875,
    "havana, ha": 2102000,
    "la habana, cu": 2102000,
    "artibonite, ht": 1260000,
    "nord-est, ht": 393967,
    "nord, ht": 728807,
    "las tunas, cu": 519850,
    "cienfuegos, cu": 415923,
    "isla de la juventud, cu": 86000,
    "saint-pierre-et-miquelon, pm": 6109,
    "guantanamo, cu": 509600,
    
    # Navajo Nation & special regions
    "navajo nation, az": 174000,
}

STATE_POPULATION_PATTERNS = [
    (re.compile(r"^(.+),\s*([A-Z]{2})$", re.IGNORECASE), "city_state"),
    (re.compile(r"^(.+) County,\s*([A-Z]{2})$", re.IGNORECASE), "county_state"),
    (re.compile(r"^(.+ Metro, .+)$", re.IGNORECASE), "metro"),
    (re.compile(r"^(.+ Area, .+)$", re.IGNORECASE), "metro"),
    (re.compile(r"^(.*Island.*)$", re.IGNORECASE), "island"),
]

SMALL_SPECIALS = {
    "world border": 0,
    "plant": 2500,
    "bay": 5000,
    "naval base": 1500,
    "afb": 1500,
    "air force base": 1500,
    "national park": 1000,
    "metro": 400000,
    "area": 200000,
    "state": None,
    "province": None,
}

PLACEHOLDER_CITY_POPULATION = 25000
PLACEHOLDER_COUNTY_POPULATION = 50000
PLACEHOLDER_ISLAND_POPULATION = 10000
PLACEHOLDER_RURAL_POPULATION = 10000


def parse_localisation(file_path):
    regex = re.compile(r"^\s*STATE_(\d+):\d+\s+\"(.+)\"\s*$")
    mapping = {}
    with open(file_path, encoding="utf-8") as f:
        for line in f:
            m = regex.match(line)
            if m:
                state_id = int(m.group(1))
                state_name = m.group(2).strip()
                mapping[state_id] = state_name
    return mapping


def parse_state_file(file_path):
    with open(file_path, encoding="utf-8") as f:
        text = f.read()
    id_match = re.search(r"^\s*id\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    manpower_match = re.search(r"^\s*manpower\s*=\s*([0-9]+)\s*$", text, re.MULTILINE)
    if not id_match:
        raise ValueError(f"Missing id in {file_path}")
    state_id = int(id_match.group(1))
    manpower = int(manpower_match.group(1)) if manpower_match else None
    return state_id, manpower


def normalize_key(name):
    key = name.strip().lower()
    key = key.replace("’", "'")
    key = key.replace("–", "-")
    key = key.replace("—", "-")
    key = re.sub(r"\s+", " ", key)
    return key


def guess_population(name):
    if not name:
        return None, "empty"

    key = normalize_key(name)
    if key.startswith("state_"):
        return None, "placeholder"
    if key in EXACT_POPULATIONS:
        return EXACT_POPULATIONS[key], "exact"

    # Generic special-case rules
    if key in SMALL_SPECIALS:
        return SMALL_SPECIALS[key], "special"

    if "world border" in key:
        return 0, "special"
    if "naval base" in key or "afp" in key or "afb" in key or "air force base" in key:
        return 1500, "special"
    if "national park" in key or ("mountains" in key and "county" not in key) or ("island" in key and "county" not in key and "isla" not in key):
        return PLACEHOLDER_ISLAND_POPULATION, "special"
    if "plant" in key:
        return 2500, "special"
    if "navajo nation" in key:
        return 174000, "exact"
    if "turks and caicos" in key:
        return 21240, "exact"
    if "british virgin" in key:
        return 20875, "exact"
    if "puerto rico" in key:
        return 3808610, "exact"
    if "bahamas" in key:
        return 303658, "exact"
    if "havana" in key or "la habana" in key:
        return 2102000, "exact"

    # State/province rules with exact matches
    if key in EXACT_POPULATIONS:
        return EXACT_POPULATIONS[key], "exact"

    # City / county heuristics
    for regex, kind in STATE_POPULATION_PATTERNS:
        m = regex.match(name)
        if not m:
            continue
        if kind == "city_state":
            return PLACEHOLDER_CITY_POPULATION, "city_fallback"
        if kind == "county_state":
            return PLACEHOLDER_COUNTY_POPULATION, "county_fallback"
        if kind == "metro":
            # Keep a larger fallback for metro names
            return 500000, "metro_fallback"
        if kind == "island":
            return PLACEHOLDER_ISLAND_POPULATION, "island_fallback"

    # Generic plain name heuristics
    if key and not key.startswith("state_"):
        if "county" in key:
            return PLACEHOLDER_COUNTY_POPULATION, "plain_county_fallback"
        if "city" in key or "metro" in key or "area" in key:
            return 500000, "plain_metro_fallback"
        if "island" in key or "bay" in key or "mountains" in key:
            return PLACEHOLDER_ISLAND_POPULATION, "plain_geography_fallback"
        if len(key.split()) <= 3:
            return PLACEHOLDER_CITY_POPULATION, "plain_name_fallback"

    return None, "unknown"


def collect_states(localisation_map, state_dir):
    # Match both standard (ID-State_ID.txt) and alternate (ID-Name.txt) naming patterns
    state_files = sorted([f for f in state_dir.glob("*.txt") if f.name.split("-")[0].isdigit()], 
                         key=lambda p: int(p.name.split("-")[0]))
    rows = []
    for path in state_files:
        state_id, manpower = parse_state_file(path)
        name = localisation_map.get(state_id, None)
        estimate, source = guess_population(name)
        rows.append({
            "id": state_id,
            "file": str(path.relative_to(ROOT)),
            "name": name or "UNKNOWN",
            "current_manpower": manpower,
            "estimated_population": estimate if estimate is not None else "",
            "estimate_source": source,
        })
    return rows


def write_csv(rows, output_path):
    fieldnames = ["id", "file", "name", "current_manpower", "estimated_population", "estimate_source"]
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def update_state_files(rows, output_csv=None):
    for row in rows:
        if not row["estimated_population"]:
            continue
        if row["current_manpower"] == row["estimated_population"]:
            continue
        path = ROOT / row["file"]
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        new_text = re.sub(r"^\s*manpower\s*=\s*[0-9]+\s*$",
                          f"\tmanpower={row['estimated_population']}",
                          text, count=1, flags=re.MULTILINE)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)

    if output_csv:
        write_csv(rows, output_csv)


def parse_args():
    parser = argparse.ArgumentParser(description="Estimate HOI4 state manpower based on state names.")
    parser.add_argument("--localisation", default=str(LOCALISATION_FILE), help="Path to state name localisation file")
    parser.add_argument("--state-dir", default=str(STATE_DIR), help="Path to history state files")
    parser.add_argument("--output", default=str(OUTPUT_CSV), help="CSV file with state estimates")
    parser.add_argument("--apply", action="store_true", help="Apply estimated population values back into state files")
    parser.add_argument("--verbose", action="store_true", help="Print summary output")
    return parser.parse_args()


def main():
    args = parse_args()
    localisation_map = parse_localisation(Path(args.localisation))
    rows = collect_states(localisation_map, Path(args.state_dir))
    write_csv(rows, Path(args.output))
    if args.verbose:
        print(f"Wrote {len(rows)} state estimate rows to {args.output}")
        unknown_count = sum(1 for row in rows if not row["estimated_population"])
        print(f"Unknown / unestimated states: {unknown_count}")
        sample = [r for r in rows if r["estimate_source"] in {"unknown", "placeholder"}][:20]
        for r in sample:
            print(f"{r['id']:>4}: {r['name']} -> {r['estimate_source']}")
    if args.apply:
        update_state_files(rows)
        if args.verbose:
            print("Updated state files with estimated population values.")

if __name__ == "__main__":
    main()
