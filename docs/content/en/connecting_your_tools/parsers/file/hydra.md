---
title: "Hydra"
toc_hide: true
---
Import JSON reports from [THC Hydra](https://github.com/vanhauser-thc/thc-hydra).

Hydra can discover weak login credentials on different types of services (e.g. RDP).

As Hydra cannot provide a severity rating (as it doesn't know how severe a weak login is at this scanned service), all imported findings will be rated 'High'.

Sample JSON report:
```json
{
    "errormessages": [
        "[ERROR] Error Message of Something",
        "[ERROR] Another Message",
        "These are very free form"
    ],
    "generator": {
        "built": "2019-03-01 14:44:22",
        "commandline": "hydra -b jsonv1 -o results.json ... ...",
        "jsonoutputversion": "1.00",
        "server": "127.0.0.1",
        "service": "http-post-form",
        "software": "Hydra",
        "version": "v8.5"
    },
    "quantityfound": 1,
    "results": [
        {
            "host": "127.0.0.1",
            "login": "bill@example.com",
            "password": "bill",
            "port": 9999,
            "service": "http-post-form"
        }
    ],
    "success": false
}
```

### Sample Scan Data
Sample Hydra scans can be found [here](https://github.com/DefectDojo/django-DefectDojo/tree/master/unittests/scans/hydra).

### Default Deduplication Hashcode Fields
By default, DefectDojo identifies duplicate Findings using these [hashcode fields](https://docs.defectdojo.com/en/working_with_findings/finding_deduplication/about_deduplication/):

- title
- description
