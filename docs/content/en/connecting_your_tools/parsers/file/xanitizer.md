---
title: "Xanitizer"
toc_hide: true
---
Import XML findings list report, preferably with parameter
\'generateDetailsInFindingsListReport=true\'.

### Sample Scan Data
Sample Xanitizer scans can be found [here](https://github.com/DefectDojo/django-DefectDojo/tree/master/unittests/scans/xanitizer).

### Default Deduplication Hashcode Fields
By default, DefectDojo identifies duplicate Findings using these [hashcode fields](https://docs.defectdojo.com/en/working_with_findings/finding_deduplication/about_deduplication/):

- title
- cwe
- line
- file path
- description
