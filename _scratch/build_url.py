import urllib.parse
def build(date):
    out = f"{date}T07:00:00"
    inw = f"{date}T18:00:00"
    base = "https://www.thetrainline.com/book/results"
    params = {
        "journeySearchType": "return",
        "origin": "urn:trainline:generic:loc:YAT3392gb",
        "destination": "urn:trainline:generic:loc:PAD3087gb",
        "outwardDate": out,
        "outwardDateType": "departAfter",
        "inwardDate": inw,
        "inwardDateType": "departAfter",
        "selectedTab": "train",
        "splitSave": "true",
        "lang": "en",
        "transportModes[]": "mixed",
    }
    return base + "?" + urllib.parse.urlencode(params)

import sys
print(build(sys.argv[1]))
