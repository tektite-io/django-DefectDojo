import logging
import re
from collections.abc import Callable

import cvss.parser
from cvss import CVSS2, CVSS3, CVSS4
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r'[ ,\'"]')  # Matches spaces, commas, single quotes, double quotes


def tag_validator(value: str | list[str], exception_class: Callable = ValidationError) -> None:
    error_messages = []

    if not value:
        return

    if isinstance(value, list):
        error_messages.extend(f"Invalid tag: '{tag}'. Tags should not contain spaces, commas, or quotes." for tag in value if TAG_PATTERN.search(tag))
    elif isinstance(value, str):
        if TAG_PATTERN.search(value):
            error_messages.append(f"Invalid tag: '{value}'. Tags should not contain spaces, commas, or quotes.")
    else:
        error_messages.append(f"Value must be a string or list of strings: {value} - {type(value)}.")

    if error_messages:
        logger.debug(f"Tag validation failed: {error_messages}")
        raise exception_class(error_messages)


def clean_tags(value: str | list[str], exception_class: Callable = ValidationError) -> str | list[str]:

    if not value:
        return value

    if isinstance(value, list):
        # Replace ALL occurrences of problematic characters in each tag
        return [TAG_PATTERN.sub("_", tag) for tag in value]

    if isinstance(value, str):
        # Replace ALL occurrences of problematic characters in the tag
        return TAG_PATTERN.sub("_", value)

    msg = f"Value must be a string or list of strings: {value} - {type(value)}."
    raise exception_class(msg)


def cvss3_validator(value: str | list[str], exception_class: Callable = ValidationError) -> None:
    logger.error("cvss3_validator called with value: %s", value)
    cvss_vectors = cvss.parser.parse_cvss_from_text(value)
    if len(cvss_vectors) > 0:
        vector_obj = cvss_vectors[0]

        if isinstance(vector_obj, CVSS3):
            # all is good
            return

        if isinstance(vector_obj, CVSS4):
            # CVSS4 is not supported yet by the parse_cvss_from_text function, but let's prepare for it anyway: https://github.com/RedHatProductSecurity/cvss/issues/53
            msg = "Unsupported CVSS(4) version detected."
            raise exception_class(msg)
        if isinstance(vector_obj, CVSS2):
            msg = "Unsupported CVSS(2) version detected."
            raise exception_class(msg)

        msg = "Unsupported CVSS version detected."
        raise exception_class(msg)

    # Explicitly raise an error if no CVSS vectors are found,
    # to avoid 'NoneType' errors during severity processing later.
    msg = "No valid CVSS vectors found by cvss.parse_cvss_from_text()"
    raise exception_class(msg)
