__author__ = "feeltheajf"

import json

from dateutil import parser

from dojo.models import Finding


class BrakemanParser:
    def get_fields(self) -> list[str]:
        """
        Return the list of fields used in the Brakeman Parser.

        Fields:
        - title: Made by joining warning_type and message provided by Brakeman Scanner.
        - description: Made by joining filename, line number, issue confidence, code, user input, and render path provided by Brakeman Scanner.
        - severity: Set to Medium regardless of context.
        - file_path: Set to file from Brakeman Scanner.
        - line: Set to line from Brakeman Scanner.
        - date: Set to end_date from Brakeman Scanner.
        - static_finding: Set to true.
        """
        return [
            "title",
            "description",
            "severity",
            "file_path",
            "line",
            "date",
            "static_finding",
        ]

    def get_dedupe_fields(self) -> list[str]:
        """
        Return the list of fields used for deduplication in the Brakeman Parser.

        Fields:
        - title: Made by joining warning_type and message provided by Brakeman Scanner.
        - line: Set to line from Brakeman Scanner.
        - file_path: Set to file from Brakeman Scanner.
        - description: Made by joining filename, line number, issue confidence, code, user input, and render path provided by Brakeman Scanner.

        NOTE: uses legacy dedupe: ['title', 'cwe', 'line', 'file_path', 'description']
        NOTE: cwe is not provided by parser.
        """
        return [
            "title",
            "line",
            "file_path",
            "description",
        ]

    def get_scan_types(self):
        return ["Brakeman Scan"]

    def get_label_for_scan_types(self, scan_type):
        return "Brakeman Scan"

    def get_description_for_scan_types(self, scan_type):
        return "Import Brakeman Scanner findings in JSON format."

    def get_findings(self, filename, test):
        if filename is None:
            return ()

        tree = filename.read()
        try:
            data = json.loads(str(tree, "utf-8"))
        except BaseException:
            data = json.loads(tree)
        dupes = {}
        find_date = parser.parse(data["scan_info"]["end_time"])

        for item in data["warnings"]:
            impact = ""
            findingdetail = ""

            title = item["warning_type"] + ". " + item["message"]

            # Finding details information
            findingdetail += "Filename: " + item["file"] + "\n"
            if item["line"] is not None:
                findingdetail += "Line number: " + str(item["line"]) + "\n"
            findingdetail += "Issue Confidence: " + item["confidence"] + "\n\n"
            if item["code"] is not None:
                findingdetail += "Code:\n" + item["code"] + "\n"
            if item["user_input"] is not None:
                findingdetail += "User input:\n" + item["user_input"] + "\n"
            if item["render_path"] is not None:
                findingdetail += "Render path details:\n"
                findingdetail += json.dumps(item["render_path"], indent=4)
            sev = "Medium"
            references = item["link"]

            dupe_key = item["fingerprint"]

            if dupe_key in dupes:
                find = dupes[dupe_key]
            else:
                dupes[dupe_key] = True

                find = Finding(
                    title=title,
                    test=test,
                    description=findingdetail,
                    severity=sev,
                    impact=impact,
                    references=references,
                    file_path=item["file"],
                    line=item["line"],
                    date=find_date,
                    static_finding=True,
                )

                dupes[dupe_key] = find

        return list(dupes.values())
