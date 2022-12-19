"""Utilities for validating and parsing JUnit XML files generated by Pytest and Spytest.

This library/script should work for any test result XML file generated by Pytest or Spytest.

CLI Usage:
% python3 junit_xml_parser.py -h
usage: junit_xml_parser.py [-h] [--validate-only] [--compact] [--output-file OUTPUT_FILE] file

Validate and convert SONiC JUnit XML files into JSON.

positional arguments:
file                  A file to validate/parse.

optional arguments:
-h, --help            show this help message and exit
--validate-only       Validate without parsing the file.
--compact, -c         Output the JSON in a compact form.
--output-file OUTPUT_FILE, -o OUTPUT_FILE
                        A file to store the JSON output in.

Examples:
python3 junit_xml_parser.py tests/files/sample_tr.xml
"""
import argparse
import glob
import json
import sys
import os

from collections import defaultdict
from datetime import datetime
from utilities import TestResultJSONValidationError
from utilities import validate_json_file

import defusedxml.ElementTree as ET


TEST_REPORT_CLIENT_VERSION = (1, 1, 0)

MAXIMUM_XML_SIZE = 20e7  # 20MB
MAXIMUM_SUMMARY_SIZE = 1024  # 1MB

# Fields found in the testsuite/root section of the JUnit XML file.
TESTSUITE_TAG = "testsuite"
REQUIRED_TESTSUITE_ATTRIBUTES = {
    ("time", float),
    ("tests", int),
    ("skipped", int),
    ("failures", int),
    ("errors", int)
}
EXTRA_XML_SUMMARY_ATTRIBUTES = {
    ("xfails", int)
}
# Fields found in the metadata/properties section of the JUnit XML file.
# FIXME: These are specific to pytest, needs to be extended to support spytest.
PROPERTIES_TAG = "properties"
PROPERTY_TAG = "property"
REQUIRED_METADATA_PROPERTIES = [
    "topology",
    "testbed",
    "timestamp",
    "host",
    "asic",
    "platform",
    "hwsku",
    "os_version",
]

# Fields found in the testcase sections of the JUnit XML file.
TESTCASE_TAG = "testcase"
REQUIRED_TESTCASE_ATTRIBUTES = [
    "classname",
    "file",
    "line",
    "name",
    "time",
]

# Fields found in the testcase/properties section of the JUnit XML file.
# FIXME: These are specific to pytest, needs to be extended to support spytest.
TESTCASE_PROPERTIES_TAG = "properties"
TESTCASE_PROPERTY_TAG = "property"
REQUIRED_TESTCASE_PROPERTIES = [
    "start",
    "end",
]

REQUIRED_TESTCASE_JSON_FIELDS = ["result", "error", "summary"]


class JUnitXMLValidationError(Exception):
    """Expected errors that are thrown while validating the contents of the JUnit XML file."""


def validate_junit_xml_stream(stream):
    """Validate that a stream containing an XML document is valid JUnit XML.

    Args:
        stream: A string containing an XML document.

    Returns:
        The root of the validated XML document.

    Raises:
        JUnitXMLValidationError: if any of the following are true:
            - The provided stream exceeds 10MB
            - The provided stream is unparseable
            - The provided stream is missing required fields
    """
    if sys.getsizeof(stream) > MAXIMUM_XML_SIZE:
        raise JUnitXMLValidationError("provided stream is too large")

    try:
        root = ET.fromstring(stream, forbid_dtd=True)
    except Exception as e:
        raise JUnitXMLValidationError(f"could not parse provided XML stream: {e}") from e

    return _validate_junit_xml(root)


def validate_junit_xml_file(document_name):
    """Validate that an XML file is valid JUnit XML.

    Args:
        document_name: The name of the document.

    Returns:
        The root of the validated XML document.

    Raises:
        JUnitXMLValidationError: if any of the following are true:
            - The provided file doesn't exist
            - The provided file exceeds 10MB
            - The provided file is unparseable
            - The provided file is missing required fields
    """
    if not os.path.exists(document_name) or not os.path.isfile(document_name):
        raise JUnitXMLValidationError("file not found")

    if os.path.getsize(document_name) > MAXIMUM_XML_SIZE:
        raise JUnitXMLValidationError("provided file is too large")

    try:
        tree = ET.parse(document_name, forbid_dtd=True)
    except Exception as e:
        raise JUnitXMLValidationError(f"could not parse {document_name}: {e}") from e

    return _validate_junit_xml(tree.getroot())


def validate_junit_xml_archive(directory_name, strict=False):
    """Validate that an XML archive contains valid JUnit XML.

    Args:
        directory_name: The name of the directory containing XML documents.

    Returns:
        A list of roots of validated XML documents.

    Raises:
        JUnitXMLValidationError: if any of the following are true:
            - The provided directory doesn't exist
            - The provided files exceed 10MB
            - Any of the provided files are unparseable
            - Any of the provided files are missing required fields
    """
    if not os.path.exists(directory_name) or not os.path.isdir(directory_name):
        print("directory {} not found".format(directory_name))
        return

    roots = []
    metadata_source = None
    metadata = {}
    doc_list = glob.glob(os.path.join(directory_name, "tr.xml"))
    doc_list += glob.glob(os.path.join(directory_name, "*test*.xml"))
    doc_list += glob.glob(os.path.join(directory_name, "**", "*test*.xml"), recursive=True)
    doc_list = set(doc_list)

    total_size = 0
    for document in doc_list:
        total_size += os.path.getsize(document)

    if total_size > MAXIMUM_XML_SIZE:
        raise JUnitXMLValidationError("provided directory is too large")

    for document in doc_list:
        try:
            root = validate_junit_xml_file(document)
            root_metadata = {k: v for k, v in _parse_test_metadata(root).items()
                             if k in REQUIRED_METADATA_PROPERTIES and k != "timestamp"}

            if root_metadata:
                # All metadata from a single test run should be identical, so we
                # just use the first one we see to validate the rest.
                if not metadata_source:
                    metadata_source = document
                    metadata = root_metadata

                if root_metadata != metadata:
                    raise JUnitXMLValidationError(f"{document} metadata differs from {metadata_source}\n"
                                                  f"{document}: {root_metadata}\n"
                                                  f"{metadata_source}: {metadata}")

            roots.append(root)
        except Exception as e:
            if strict:
                raise JUnitXMLValidationError(f"could not parse {document}: {e}") from e

            print(f"could not parse {document}: {e} - skipping")

    if not roots:
        print("provided directory {} does not contain any valid XML files".format(directory_name))
    return roots


def validate_junit_xml_path(path, strict=False):
    if os.path.isfile(path):
        roots = [validate_junit_xml_file(path)]
    else:
        roots = validate_junit_xml_archive(path, strict)

    return roots


def _validate_junit_xml(root):
    _validate_test_summary(root)
    _validate_test_metadata(root)
    _validate_test_cases(root)

    return root


def _validate_test_summary(root):
    if root.tag != TESTSUITE_TAG:
        raise JUnitXMLValidationError(f"{TESTSUITE_TAG} tag not found on root element")

    for xml_field, expected_type in REQUIRED_TESTSUITE_ATTRIBUTES:
        if xml_field not in root.keys():
            raise JUnitXMLValidationError(f"{xml_field} not found in <{TESTSUITE_TAG}> element")

        try:
            expected_type(root.get(xml_field))
        except Exception as e:
            raise JUnitXMLValidationError(
                f"invalid type for {xml_field} in {TESTSUITE_TAG}> element: "
                f"expected a number, received "
                f'"{root.get(xml_field)}"'
            ) from e


def _validate_test_metadata(root):
    properties_element = root.find(PROPERTIES_TAG)

    if not properties_element:
        return

    seen_properties = []
    for prop in properties_element.iterfind(PROPERTY_TAG):
        property_name = prop.get("name", None)

        if not property_name:
            continue

        if property_name not in REQUIRED_METADATA_PROPERTIES:
            continue

        if property_name in seen_properties:
            raise JUnitXMLValidationError(
                f"duplicate metadata element: {property_name} seen more than once"
            )

        property_value = prop.get("value", None)

        if property_value is None:  # Some fields may be empty
            raise JUnitXMLValidationError(
                f'invalid metadata element: no "value" field provided for {property_name}'
            )

        seen_properties.append(property_name)

    if set(seen_properties) < set(REQUIRED_METADATA_PROPERTIES):
        raise JUnitXMLValidationError("missing metadata element(s)")

def _validate_test_case_properties(root):
    testcase_properties_element = root.find(TESTCASE_PROPERTIES_TAG)

    if not testcase_properties_element:
        return

    seen_testcase_properties = []
    for testcase_prop in testcase_properties_element.iterfind(TESTCASE_PROPERTY_TAG):
        testcase_property_name = testcase_prop.get("name", None)

        if not testcase_property_name:
            continue

        if testcase_property_name not in REQUIRED_TESTCASE_PROPERTIES:
            continue

        if testcase_property_name in seen_testcase_properties:
            raise JUnitXMLValidationError(
                f"duplicate metadata element: {testcase_property_name} seen more than once"
            )

        testcase_property_value = testcase_prop.get("value", None)

        if testcase_property_value is None:  # Some fields may be empty
            raise JUnitXMLValidationError(
                f'invalid metadata element: no "value" field provided for {testcase_property_name}'
            )

        seen_testcase_properties.append(testcase_property_name)

    missing_testcase_property = set(seen_testcase_properties) < set(REQUIRED_TESTCASE_PROPERTIES)
    if missing_testcase_property:
        print("missing testcase property: {}".format(list(missing_testcase_property)))

def _validate_test_cases(root):
    def _validate_test_case(test_case):
        for attribute in REQUIRED_TESTCASE_ATTRIBUTES:
            if attribute not in test_case.keys():
                raise JUnitXMLValidationError(
                    f'"{attribute}" not found in test case '
                    f"\"{test_case.get('name', 'Name Not Found')}\""
                )
        _validate_test_case_properties(test_case)

    cases = root.findall(TESTCASE_TAG)

    for test_case in cases:
        _validate_test_case(test_case)


def parse_test_result(roots):
    """Parse a given XML document into JSON.

    Args:
        root: The root of the XML document to parse.

    Returns:
        A dict containing the parsed test result.
    """
    test_result_json = defaultdict(dict)
    if not roots:
        print("No XML file needs to be parsed or the file is empty.")
        return

    for root in roots:
        test_result_json["test_metadata"] = _update_test_metadata(test_result_json["test_metadata"],
                                                                  _parse_test_metadata(root))
        test_cases = _parse_test_cases(root)
        test_result_json["test_cases"] = _update_test_cases(test_result_json["test_cases"], test_cases)
        test_result_json["test_summary"] = _update_test_summary(test_result_json["test_summary"],
                                                                _extract_test_summary(test_cases))

    return test_result_json


def _parse_test_summary(root):
    test_result_summary = {}
    for attribute, _ in REQUIRED_TESTSUITE_ATTRIBUTES:
        test_result_summary[attribute] = root.get(attribute)

    return test_result_summary


def _extract_test_summary(test_cases):
    test_result_summary = defaultdict(int)
    for _, cases in test_cases.items():
        for case in cases:
            # Error may occur along with other test results, to count error separately. 
            # The result field is unique per test case, either error or failure.
            # xfails is the counter for all kinds of xfail results (include success/failure/error/skipped)
            test_result_summary["tests"] += 1
            test_result_summary["failures"] += case["result"] == "failure" or case["result"] == "error"
            test_result_summary["skipped"] += case["result"] == "skipped"
            test_result_summary["errors"] += case["error"]
            test_result_summary["time"] += float(case["time"])
            test_result_summary["xfails"] += case["result"] == "xfail_failure" or \
                                             case["result"] == "xfail_error" or \
                                             case["result"] == "xfail_skipped" or \
                                             case["result"] == "xfail_success"

    test_result_summary = {k: str(v) for k, v in test_result_summary.items()}
    return test_result_summary


def _parse_test_metadata(root):
    properties_element = root.find(PROPERTIES_TAG)

    if not properties_element:
        return {}

    test_result_metadata = {}
    for prop in properties_element.iterfind(PROPERTY_TAG):
        if prop.get("value"):
            test_result_metadata[prop.get("name")] = prop.get("value")

    return test_result_metadata

def _parse_testcase_properties(root):
    testcase_properties_element = root.find(TESTCASE_PROPERTIES_TAG)

    if not testcase_properties_element:
        return {}

    testcase_properties = {}
    for testcase_prop in testcase_properties_element.iterfind(TESTCASE_PROPERTY_TAG):
        if testcase_prop.get("value"):
            testcase_properties[testcase_prop.get("name")] = testcase_prop.get("value")

    return testcase_properties

def _parse_test_cases(root):
    test_case_results = defaultdict(list)

    def _parse_test_case(test_case):
        result = {}

        # FIXME: This is specific to pytest, needs to be extended to support spytest.
        test_class_tokens = test_case.get("classname").split(".")
        feature = test_class_tokens[0]

        for attribute in REQUIRED_TESTCASE_ATTRIBUTES:
            result[attribute] = test_case.get(attribute)
        for attribute in REQUIRED_TESTCASE_PROPERTIES:
            testcase_properties = _parse_testcase_properties(test_case)
            if attribute in testcase_properties:
                result[attribute] = testcase_properties[attribute]

        # NOTE: "if failure" and "if error" does not work with the ETree library.
        failure = test_case.find("failure")
        error = test_case.find("error")
        skipped = test_case.find("skipped")

        # Any test which marked as xfail will drop out a property to the report xml file.
        # Add prefix "xfail_" to tests which are marked with xfail
        properties_element = test_case.find(PROPERTIES_TAG)
        xfail_case = ""
        if properties_element:
            for prop in properties_element.iterfind(PROPERTY_TAG):
                if prop.get("name") == "xfail":
                    xfail_case = "xfail_"
                    break

        # NOTE: "error" is unique in that it can occur alongside a succesful, failed, or skipped test result.
        # Because of this, we track errors separately so that the error can be correlated with the stage it
        # occurred.
        # By looking into test results from past 300 days, error only occur with skipped test result.
        #
        # If there is *only* an error tag we note that as well, as this indicates that the framework
        # errored out during setup or teardown.
        if failure is not None:
            result["result"] = "{}failure".format(xfail_case)
            summary = failure.get("message", "")
        elif skipped is not None:
            result["result"] = "{}skipped".format(xfail_case)
            summary = skipped.get("message", "")
        elif error is not None:
            result["result"] = "{}error".format(xfail_case)
            summary = error.get("message", "")
        else:
            result["result"] = "{}success".format(xfail_case)
            summary = ""

        result["summary"] = summary[:min(len(summary), MAXIMUM_SUMMARY_SIZE)]
        result["error"] = error is not None

        return feature, result

    for test_case in root.findall("testcase"):
        feature, result = _parse_test_case(test_case)
        test_case_results[feature].append(result)

    return dict(test_case_results)


def _update_test_summary(current, update):
    if not current:
        return update.copy()

    new_summary = {}
    for attribute, attr_type in REQUIRED_TESTSUITE_ATTRIBUTES:
        new_summary[attribute] = str(round(attr_type(current.get(attribute, 0)) + attr_type(update.get(attribute, 0)), 3))

    for attribute, attr_type in EXTRA_XML_SUMMARY_ATTRIBUTES:
        new_summary[attribute] = str(round(attr_type(current.get(attribute, 0)) + attr_type(update.get(attribute, 0)), 3))

    return new_summary


def _update_test_metadata(current, update):
    # Case 1: On the very first update, current will be empty since we haven't seen any results yet.
    if not current:
        return update.copy()

    # Case 2: For test cases that are 100% skipped there will be no metadata added, so we need to
    # default to current.
    if not update:
        return current.copy()

    # Case 3: For all other cases, take the earliest timestamp and default everything else to update.
    new_metadata = {}
    for prop in REQUIRED_METADATA_PROPERTIES:
        if prop == "timestamp":
            new_metadata[prop] = str(min(datetime.strptime(current[prop], "%Y-%m-%d %H:%M:%S.%f"),
                                         datetime.strptime(update[prop], "%Y-%m-%d %H:%M:%S.%f")))
        else:
            new_metadata[prop] = update[prop]

    return new_metadata


def _update_test_cases(current, update):
    if not current:
        return update.copy()

    new_cases = current.copy()
    for group, cases in update.items():
        updated_cases = cases.copy()
        if group in new_cases:
            updated_cases += new_cases[group]

        new_cases[group] = updated_cases

    return new_cases


def validate_junit_json_file(path):
    """Validate that a JSON file is a valid test report.

    Args:
        path: The path to the JSON file.

    Returns:
        The validated JSON file.

    Raises:
        TestResultJSONValidationError: if any of the following are true:
            - The provided file doesn't exist
            - The provided file is unparseable
            - The provided file is missing required fields
    """
    test_result_json = validate_json_file(path)
    if not test_result_json:
        return
    _validate_json_metadata(test_result_json)
    _validate_json_summary(test_result_json)
    _validate_json_cases(test_result_json)

    return test_result_json


def _validate_json_metadata(test_result_json):
    if "test_metadata" not in test_result_json:
        raise TestResultJSONValidationError("test_metadata section not found in provided JSON file")

    seen_properties = []
    for prop, value in test_result_json["test_metadata"].items():
        if prop not in REQUIRED_METADATA_PROPERTIES:
            continue

        if prop in seen_properties:
            raise TestResultJSONValidationError(
                f"duplicate metadata element: {prop} seen more than once"
            )

        if value is None:  # Some fields may be empty
            raise TestResultJSONValidationError(
                f'invalid metadata element: no "value" field provided for {prop}'
            )

        seen_properties.append(prop)

    if set(seen_properties) < set(REQUIRED_METADATA_PROPERTIES):
        raise TestResultJSONValidationError("missing metadata element(s)")


def _validate_json_summary(test_result_json):
    if "test_summary" not in test_result_json:
        raise TestResultJSONValidationError("test_summary section not found in provided JSON file")

    summary = test_result_json["test_summary"]

    for field, expected_type in REQUIRED_TESTSUITE_ATTRIBUTES:
        if field not in summary:
            raise TestResultJSONValidationError(f"{field} not found in test_summary section")

        try:
            expected_type(summary[field])
        except Exception as e:
            raise TestResultJSONValidationError(
                f"invalid type for {field} in test_summary section: "
                f"expected a number, received "
                f'"{summary[field]}"'
            ) from e


def _validate_json_cases(test_result_json):
    if "test_cases" not in test_result_json:
        raise TestResultJSONValidationError("test_cases section not found in provided JSON file")

    def _validate_test_case(test_case):
        for attribute in REQUIRED_TESTCASE_ATTRIBUTES + REQUIRED_TESTCASE_JSON_FIELDS:
            if attribute not in test_case:
                raise TestResultJSONValidationError(
                    f'"{attribute}" not found in test case '
                    f"\"{test_case.get('name', 'Name Not Found')}\""
                )
        for attribute in REQUIRED_TESTCASE_PROPERTIES:
            if attribute not in test_case:
                print("missing testcase property {} in testcase {}".format(attribute, test_case["classname"]))

    for _, feature in test_result_json["test_cases"].items():
        for test_case in feature:
            _validate_test_case(test_case)


def _run_script():
    parser = argparse.ArgumentParser(
        description="Validate and convert SONiC JUnit XML files into JSON.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:
python3 junit_xml_parser.py tests/files/sample_tr.xml
""",
    )
    parser.add_argument("file_name", metavar="file", type=str, help="A file to validate/parse.")
    parser.add_argument(
        "--validate-only", action="store_true", help="Validate without parsing the file.",
    )
    parser.add_argument(
        "--compact", "-c", action="store_true", help="Output the JSON in a compact form.",
    )
    parser.add_argument(
        "--output-file", "-o", type=str, help="A file to store the JSON output in.",
    )
    parser.add_argument(
        "--directory", "-d", action="store_true", help="Provide a directory instead of a single file."
    )
    parser.add_argument(
        "--strict",
        "-s",
        action="store_true",
        help="Fail validation checks if ANY file in a given directory is not parseable."
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="Load an existing test result JSON file from path_name. Will perform validation only regardless of --validate-only option.",
    )

    args = parser.parse_args()

    try:
        if args.json:
            validate_junit_json_file(args.file_name)
        elif args.directory:
            roots = validate_junit_xml_archive(args.file_name, args.strict)
        else:
            roots = [validate_junit_xml_file(args.file_name)]
    except JUnitXMLValidationError as e:
        print(f"XML validation failed: {e}")
        sys.exit(1)
    except TestResultJSONValidationError as e:
        print(f"JSON validation failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error occured during validation: {e}")
        sys.exit(2)

    if args.validate_only or args.json:
        print(f"{args.file_name} validated succesfully!")
        sys.exit(0)

    test_result_json = parse_test_result(roots)
    if test_result_json is None:
        print("XML file doesn't exist or no data in the file.")
        sys.exit(1)

    if args.compact:
        output = json.dumps(test_result_json, separators=(",", ":"), sort_keys=True)
    else:
        output = json.dumps(test_result_json, indent=4, sort_keys=True)

    if args.output_file:
        with open(args.output_file, "w+") as output_file:
            output_file.write(output)
    else:
        print(output)


if __name__ == "__main__":
    _run_script()
