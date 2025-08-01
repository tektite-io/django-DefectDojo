import logging
import os
import pickle
import re
import warnings
from datetime import date, datetime
from pathlib import Path

import tagulous
from crispy_forms.bootstrap import InlineCheckboxes, InlineRadios
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout
from crum import get_current_user
from dateutil.relativedelta import relativedelta
from django import forms
from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.auth.password_validation import validate_password
from django.core import validators
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.forms import modelformset_factory
from django.forms.widgets import Select, Widget
from django.urls import reverse
from django.utils import timezone
from django.utils.dates import MONTHS
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from polymorphic.base import ManagerInheritanceWarning
from tagulous.forms import TagField

import dojo.jira_link.helper as jira_helper
from dojo.authorization.authorization import user_has_configuration_permission
from dojo.authorization.roles_permissions import Permissions
from dojo.endpoint.utils import endpoint_filter, endpoint_get_or_create, validate_endpoints_to_add
from dojo.engagement.queries import get_authorized_engagements
from dojo.finding.queries import get_authorized_findings
from dojo.group.queries import get_authorized_groups, get_group_member_roles
from dojo.models import (
    EFFORT_FOR_FIXING_CHOICES,
    SEVERITY_CHOICES,
    Announcement,
    Answered_Survey,
    App_Analysis,
    Benchmark_Product,
    Benchmark_Product_Summary,
    Benchmark_Requirement,
    Check_List,
    Choice,
    ChoiceAnswer,
    ChoiceQuestion,
    Cred_Mapping,
    Cred_User,
    Development_Environment,
    Dojo_Group,
    Dojo_Group_Member,
    Dojo_User,
    DojoMeta,
    Endpoint,
    Engagement,
    Engagement_Presets,
    Engagement_Survey,
    FileUpload,
    Finding,
    Finding_Group,
    Finding_Template,
    General_Survey,
    GITHUB_Conf,
    GITHUB_Issue,
    GITHUB_PKey,
    Global_Role,
    JIRA_Instance,
    JIRA_Issue,
    JIRA_Project,
    Note_Type,
    Notes,
    Notification_Webhooks,
    Notifications,
    Objects_Product,
    Product,
    Product_API_Scan_Configuration,
    Product_Group,
    Product_Member,
    Product_Type,
    Product_Type_Group,
    Product_Type_Member,
    Question,
    Regulation,
    Risk_Acceptance,
    SLA_Configuration,
    Stub_Finding,
    System_Settings,
    Test,
    Test_Type,
    TextAnswer,
    TextQuestion,
    Tool_Configuration,
    Tool_Product_Settings,
    Tool_Type,
    User,
    UserContactInfo,
)
from dojo.product.queries import get_authorized_products
from dojo.product_type.queries import get_authorized_product_types
from dojo.tools.factory import get_choices_sorted, requires_file, requires_tool_type
from dojo.user.queries import get_authorized_users, get_authorized_users_for_product_and_product_type
from dojo.user.utils import get_configuration_permissions_fields
from dojo.utils import (
    get_password_requirements_string,
    get_product,
    get_system_setting,
    is_finding_groups_enabled,
    is_scan_file_too_large,
)
from dojo.validators import tag_validator
from dojo.widgets import TableCheckboxWidget

logger = logging.getLogger(__name__)

RE_DATE = re.compile(r"(\d{4})-(\d\d?)-(\d\d?)$")

FINDING_STATUS = (("verified", "Verified"),
                  ("false_p", "False Positive"),
                  ("duplicate", "Duplicate"),
                  ("out_of_scope", "Out of Scope"))

vulnerability_ids_field = forms.CharField(max_length=5000,
    required=False,
    label="Vulnerability Ids",
    help_text="Ids of vulnerabilities in security advisories associated with this finding. Can be Common Vulnerabilities and Exposures (CVE) or from other sources."
                "You may enter one vulnerability id per line.",
    widget=forms.widgets.Textarea(attrs={"rows": "3", "cols": "400"}))

EFFORT_FOR_FIXING_INVALID_CHOICE = _("Select valid choice: Low,Medium,High")


class MultipleSelectWithPop(forms.SelectMultiple):
    def render(self, name, *args, **kwargs):
        html = super().render(name, *args, **kwargs)
        popup_plus = '<div class="input-group dojo-input-group">' + html + '<span class="input-group-btn"><a href="/' + name + '/add" class="btn btn-primary" class="add-another" id="add_id_' + name + '" onclick="return showAddAnotherPopup(this);"><span class="glyphicon glyphicon-plus"></span></a></span></div>'

        return mark_safe(popup_plus)


class MonthYearWidget(Widget):

    """
    A Widget that splits date input into two <select> boxes for month and year,
    with 'day' defaulting to the first of the month.

    Based on SelectDateWidget, in

    django/trunk/django/forms/extras/widgets.py
    """

    none_value = (0, "---")
    month_field = "%s_month"
    year_field = "%s_year"

    def __init__(self, attrs=None, years=None, *, required=True):
        # years is an optional list/tuple of years to use in the
        # "year" select box.
        self.attrs = attrs or {}
        self.required = required
        if years:
            self.years = years
        else:
            this_year = date.today().year
            self.years = list(range(this_year - 10, this_year + 1))

    def render(self, name, value, attrs=None, renderer=None):
        try:
            year_val, month_val = value.year, value.month
        except AttributeError:
            year_val = month_val = None
            if isinstance(value, str):
                match = RE_DATE.match(value)
                if match:
                    year_val, month_val = match[1], match[2]

        output = []

        id_ = self.attrs.get("id", f"id_{name}")

        month_choices = list(MONTHS.items())
        if not (self.required and value):
            month_choices.append(self.none_value)
        month_choices.sort()
        local_attrs = self.build_attrs({"id": self.month_field % id_})
        s = Select(choices=month_choices)
        select_html = s.render(self.month_field % name, month_val, local_attrs)

        output.append(select_html)

        year_choices = [(i, i) for i in self.years]
        if not (self.required and value):
            year_choices.insert(0, self.none_value)
        local_attrs["id"] = self.year_field % id_
        s = Select(choices=year_choices)
        select_html = s.render(self.year_field % name, year_val, local_attrs)
        output.append(select_html)

        return mark_safe("\n".join(output))

    @classmethod
    def id_for_label(cls, id_):
        return f"{id_}_month"

    def value_from_datadict(self, data, files, name):
        y = data.get(self.year_field % name)
        m = data.get(self.month_field % name)
        if y == m == "0":
            return None
        if y and m:
            return f"{y}-{m}-{1}"
        return data.get(name, None)


class Product_TypeForm(forms.ModelForm):
    description = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=False)

    class Meta:
        model = Product_Type
        fields = ["name", "description", "critical_product", "key_product"]


class Delete_Product_TypeForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Product_Type
        fields = ["id"]


class Edit_Product_Type_MemberForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product_type"].disabled = True
        self.fields["user"].queryset = Dojo_User.objects.order_by("first_name", "last_name")
        self.fields["user"].disabled = True

    class Meta:
        model = Product_Type_Member
        fields = ["product_type", "user", "role"]


class Add_Product_Type_MemberForm(forms.ModelForm):
    users = forms.ModelMultipleChoiceField(queryset=Dojo_User.objects.none(), required=True, label="Users")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_members = Product_Type_Member.objects.filter(product_type=self.initial["product_type"]).values_list("user", flat=True)
        self.fields["users"].queryset = Dojo_User.objects.exclude(
            Q(is_superuser=True)
            | Q(id__in=current_members)).exclude(is_active=False).order_by("first_name", "last_name")
        self.fields["product_type"].disabled = True

    class Meta:
        model = Product_Type_Member
        fields = ["product_type", "users", "role"]


class Add_Product_Type_Member_UserForm(forms.ModelForm):
    product_types = forms.ModelMultipleChoiceField(queryset=Product_Type.objects.none(), required=True, label="Product Types")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_members = Product_Type_Member.objects.filter(user=self.initial["user"]).values_list("product_type", flat=True)
        self.fields["product_types"].queryset = get_authorized_product_types(Permissions.Product_Type_Member_Add_Owner) \
            .exclude(id__in=current_members)
        self.fields["user"].disabled = True

    class Meta:
        model = Product_Type_Member
        fields = ["product_types", "user", "role"]


class Delete_Product_Type_MemberForm(Edit_Product_Type_MemberForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].disabled = True


class Test_TypeForm(forms.ModelForm):
    class Meta:
        model = Test_Type
        exclude = ["dynamically_generated"]


class Development_EnvironmentForm(forms.ModelForm):
    class Meta:
        model = Development_Environment
        fields = ["name"]


class Delete_Dev_EnvironmentForm(forms.ModelForm):
    class Meta:
        model = Development_Environment
        fields = ["id"]


class ProductForm(forms.ModelForm):
    name = forms.CharField(max_length=255, required=True)
    description = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=True)

    prod_type = forms.ModelChoiceField(label="Product Type",
                                       queryset=Product_Type.objects.none(),
                                       required=True)

    sla_configuration = forms.ModelChoiceField(label="SLA Configuration",
                                        queryset=SLA_Configuration.objects.all(),
                                        required=True,
                                        initial="Default")

    product_manager = forms.ModelChoiceField(queryset=Dojo_User.objects.exclude(is_active=False).order_by("first_name", "last_name"), required=False)
    technical_contact = forms.ModelChoiceField(queryset=Dojo_User.objects.exclude(is_active=False).order_by("first_name", "last_name"), required=False)
    team_manager = forms.ModelChoiceField(queryset=Dojo_User.objects.exclude(is_active=False).order_by("first_name", "last_name"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["prod_type"].queryset = get_authorized_product_types(Permissions.Product_Type_Add_Product)
        if prod_type_id := kwargs.get("instance", Product()).prod_type_id:  # we are editing existing instance
            self.fields["prod_type"].queryset |= Product_Type.objects.filter(pk=prod_type_id)  # even if user does not have permission for any other ProdType we need to add at least assign ProdType to make form submittable (otherwise empty list was here which generated invalid form)

        # if this product has findings being asynchronously updated, disable the sla config field
        if self.instance.async_updating:
            self.fields["sla_configuration"].disabled = True
            self.fields["sla_configuration"].widget.attrs["message"] = (
                "Finding SLA expiration dates are currently being recalculated. "
                "This field cannot be changed until the calculation is complete."
            )

    class Meta:
        model = Product
        fields = ["name", "description", "tags", "product_manager", "technical_contact", "team_manager", "prod_type", "sla_configuration", "regulations",
                "business_criticality", "platform", "lifecycle", "origin", "user_records", "revenue", "external_audience", "enable_product_tag_inheritance",
                "internet_accessible", "enable_simple_risk_acceptance", "enable_full_risk_acceptance", "disable_sla_breach_notifications"]

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")


class DeleteProductForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Product
        fields = ["id"]


class EditFindingGroupForm(forms.ModelForm):
    name = forms.CharField(max_length=255, required=True, label="Finding Group Name")
    jira_issue = forms.CharField(max_length=255, required=False, label="Linked JIRA Issue",
                                 help_text="Leave empty and check push to jira to create a new JIRA issue for this finding group.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import dojo.jira_link.helper as jira_helper

        self.fields["push_to_jira"] = forms.BooleanField()
        self.fields["push_to_jira"].required = False
        self.fields["push_to_jira"].help_text = "Checking this will overwrite content of your JIRA issue, or create one."

        self.fields["push_to_jira"].label = "Push to JIRA"

        if hasattr(self.instance, "has_jira_issue") and self.instance.has_jira_issue:
            jira_url = jira_helper.get_jira_url(self.instance)
            self.fields["jira_issue"].initial = jira_url
            self.fields["push_to_jira"].widget.attrs["checked"] = "checked"

    class Meta:
        model = Finding_Group
        fields = ["name"]


class DeleteFindingGroupForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Finding_Group
        fields = ["id"]


class Edit_Product_MemberForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].disabled = True
        self.fields["user"].queryset = Dojo_User.objects.order_by("first_name", "last_name")
        self.fields["user"].disabled = True

    class Meta:
        model = Product_Member
        fields = ["product", "user", "role"]


class Add_Product_MemberForm(forms.ModelForm):
    users = forms.ModelMultipleChoiceField(queryset=Dojo_User.objects.none(), required=True, label="Users")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].disabled = True
        current_members = Product_Member.objects.filter(product=self.initial["product"]).values_list("user", flat=True)
        self.fields["users"].queryset = Dojo_User.objects.exclude(
            Q(is_superuser=True)
            | Q(id__in=current_members)).exclude(is_active=False).order_by("first_name", "last_name")

    class Meta:
        model = Product_Member
        fields = ["product", "users", "role"]


class Add_Product_Member_UserForm(forms.ModelForm):
    products = forms.ModelMultipleChoiceField(queryset=Product.objects.none(), required=True, label="Products")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_members = Product_Member.objects.filter(user=self.initial["user"]).values_list("product", flat=True)
        self.fields["products"].queryset = get_authorized_products(Permissions.Product_Member_Add_Owner) \
            .exclude(id__in=current_members)
        self.fields["user"].disabled = True

    class Meta:
        model = Product_Member
        fields = ["products", "user", "role"]


class Delete_Product_MemberForm(Edit_Product_MemberForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].disabled = True


class NoteTypeForm(forms.ModelForm):
    description = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=True)

    class Meta:
        model = Note_Type
        fields = ["name", "description", "is_single", "is_mandatory"]


class EditNoteTypeForm(NoteTypeForm):

    def __init__(self, *args, **kwargs):
        is_single = kwargs.pop("is_single")
        super().__init__(*args, **kwargs)
        if is_single is False:
            self.fields["is_single"].widget = forms.HiddenInput()


class DisableOrEnableNoteTypeForm(NoteTypeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].disabled = True
        self.fields["description"].disabled = True
        self.fields["is_single"].disabled = True
        self.fields["is_mandatory"].disabled = True
        self.fields["is_active"].disabled = True

    class Meta:
        model = Note_Type
        fields = "__all__"


class DojoMetaDataForm(forms.ModelForm):
    value = forms.CharField(widget=forms.Textarea(attrs={}),
                            required=True)

    def full_clean(self):
        super().full_clean()
        try:
            self.instance.validate_unique()
        except ValidationError:
            msg = "A metadata entry with the same name exists already for this object."
            self.add_error("name", msg)

    class Meta:
        model = DojoMeta
        fields = "__all__"


class ImportScanForm(forms.Form):
    active_verified_choices = [("not_specified", "Not specified (default)"),
                               ("force_to_true", "Force to True"),
                               ("force_to_false", "Force to False")]
    scan_date = forms.DateTimeField(
        required=False,
        label="Scan Completion Date",
        help_text="Scan completion date will be used on all findings.",
        widget=forms.TextInput(attrs={"class": "datepicker"}))
    minimum_severity = forms.ChoiceField(help_text="Minimum severity level to be imported",
                                         required=True,
                                         choices=SEVERITY_CHOICES)
    active = forms.ChoiceField(required=True, choices=active_verified_choices,
                               help_text="Force findings to be active/inactive, or default to the original tool")
    verified = forms.ChoiceField(required=True, choices=active_verified_choices,
                               help_text="Force findings to be verified/not verified, or default to the original tool")

    # help_do_not_reactivate = 'Select if the import should ignore active findings from the report, useful for triage-less scanners. Will keep existing findings closed, without reactivating them. For more information check the docs.'
    # do_not_reactivate = forms.BooleanField(help_text=help_do_not_reactivate, required=False)
    scan_type = forms.ChoiceField(required=True, choices=get_choices_sorted)
    environment = forms.ModelChoiceField(
        queryset=Development_Environment.objects.all().order_by("name"))
    endpoints = forms.ModelMultipleChoiceField(Endpoint.objects, required=False, label="Systems / Endpoints")
    endpoints_to_add = forms.CharField(max_length=5000, required=False, label="Endpoints to add",
                               help_text="The IP address, host name or full URL. You may enter one endpoint per line. "
                                         "Each must be valid.",
                               widget=forms.widgets.Textarea(attrs={"rows": "3", "cols": "400"}))
    version = forms.CharField(max_length=100, required=False, help_text="Version that was scanned.")
    branch_tag = forms.CharField(max_length=100, required=False, help_text="Branch or Tag that was scanned.")
    commit_hash = forms.CharField(max_length=100, required=False, help_text="Commit that was scanned.")
    build_id = forms.CharField(max_length=100, required=False, help_text="ID of the build that was scanned.")
    api_scan_configuration = forms.ModelChoiceField(Product_API_Scan_Configuration.objects, required=False, label="API Scan Configuration")
    service = forms.CharField(max_length=200, required=False,
        help_text="A service is a self-contained piece of functionality within a Product. "
                  "This is an optional field which is used in deduplication and closing of old findings when set.")
    source_code_management_uri = forms.URLField(max_length=600, required=False, help_text="Resource link to source code")
    tags = TagField(required=False, help_text="Add tags that help describe this scan.  "
                    "Choose from the list or add new tags. Press Enter key to add.")
    file = forms.FileField(widget=forms.widgets.FileInput(
        attrs={"accept": ".xml, .csv, .nessus, .json, .jsonl, .html, .js, .zip, .xlsx, .txt, .sarif"}),
        label="Choose report file",
        allow_empty_file=True,
        required=False)

    # Close Old Findings has changed. The default is engagement only, and it requires a second flag to expand to the product scope.
    # Exposing the choice as two different check boxes.
    # If 'close_old_findings_product_scope' is selected, the backend will ensure that both flags are set.
    close_old_findings = forms.BooleanField(help_text="Old findings no longer present in the new report get closed as mitigated when importing. "
                                                        "If service has been set, only the findings for this service will be closed. "
                                                        "This only affects findings within the same engagement.",
                                            label="Close old findings within this engagement",
                                            required=False,
                                            initial=False)
    close_old_findings_product_scope = forms.BooleanField(help_text="Old findings no longer present in the new report get closed as mitigated when importing. "
                                                        "If service has been set, only the findings for this service will be closed. "
                                                        "This only affects findings within the same product.",
                                            label="Close old findings within this product",
                                            required=False,
                                            initial=False)
    apply_tags_to_findings = forms.BooleanField(
        help_text="If set to True, the tags will be applied to the findings",
        label="Apply Tags to Findings",
        required=False,
        initial=False,
    )
    apply_tags_to_endpoints = forms.BooleanField(
        help_text="If set to True, the tags will be applied to the endpoints",
        label="Apply Tags to Endpoints",
        required=False,
        initial=False,
    )

    if is_finding_groups_enabled():
        group_by = forms.ChoiceField(required=False, choices=Finding_Group.GROUP_BY_OPTIONS, help_text="Choose an option to automatically group new findings by the chosen option.")
        create_finding_groups_for_all_findings = forms.BooleanField(help_text="If unchecked, finding groups will only be created when there is more than one grouped finding", required=False, initial=True)

    def __init__(self, *args, **kwargs):
        environment = kwargs.pop("environment", None)
        endpoints = kwargs.pop("endpoints", None)
        api_scan_configuration = kwargs.pop("api_scan_configuration", None)
        super().__init__(*args, **kwargs)
        self.fields["active"].initial = self.active_verified_choices[0]
        self.fields["verified"].initial = self.active_verified_choices[0]
        if environment:
            self.fields["environment"].initial = environment
        if endpoints:
            self.fields["endpoints"].queryset = endpoints
        if api_scan_configuration:
            self.fields["api_scan_configuration"].queryset = api_scan_configuration
        # couldn't find a cleaner way to add empty default
        if "group_by" in self.fields:
            choices = self.fields["group_by"].choices
            choices.insert(0, ("", "---------"))
            self.fields["group_by"].choices = choices

        self.endpoints_to_add_list = []

    def clean(self):
        cleaned_data = super().clean()
        scan_type = cleaned_data.get("scan_type")
        file = cleaned_data.get("file")
        tool_type = requires_tool_type(scan_type)
        if requires_file(scan_type) and not file:
            msg = _("Uploading a Report File is required for %s") % scan_type
            raise forms.ValidationError(msg)
        if file and is_scan_file_too_large(file):
            msg = _("Report file is too large. Maximum supported size is %d MB") % settings.SCAN_FILE_MAX_SIZE
            raise forms.ValidationError(msg)
        if tool_type:
            api_scan_configuration = cleaned_data.get("api_scan_configuration")
            if api_scan_configuration and tool_type != api_scan_configuration.tool_configuration.tool_type.name:
                msg = f"API scan configuration must be of tool type {tool_type}"
                raise forms.ValidationError(msg)

        endpoints_to_add_list, errors = validate_endpoints_to_add(cleaned_data["endpoints_to_add"])
        if errors:
            raise forms.ValidationError(errors)
        self.endpoints_to_add_list = endpoints_to_add_list

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    # date can only be today or in the past, not the future
    def clean_scan_date(self):
        date = self.cleaned_data.get("scan_date", None)
        if date and date.date() > datetime.today().date():
            msg = "The date cannot be in the future!"
            raise forms.ValidationError(msg)
        return date

    def get_scan_type(self):
        return self.cleaned_data["scan_type"]


class ReImportScanForm(forms.Form):
    active_verified_choices = [("not_specified", "Not specified (default)"),
                               ("force_to_true", "Force to True"),
                               ("force_to_false", "Force to False")]
    scan_date = forms.DateTimeField(
        required=False,
        label="Scan Completion Date",
        help_text="Scan completion date will be used on all findings.",
        widget=forms.TextInput(attrs={"class": "datepicker"}))
    minimum_severity = forms.ChoiceField(help_text="Minimum severity level to be imported",
                                         required=True,
                                         choices=SEVERITY_CHOICES[0:4])
    active = forms.ChoiceField(required=True, choices=active_verified_choices,
                               help_text="Force findings to be active/inactive, or default to the original tool")
    verified = forms.ChoiceField(required=True, choices=active_verified_choices,
                             help_text="Force findings to be verified/not verified, or default to the original tool")

    help_do_not_reactivate = "Select if the import should ignore active findings from the report, useful for triage-less scanners. Will keep existing findings closed, without reactivating them. For more information check the docs."
    do_not_reactivate = forms.BooleanField(help_text=help_do_not_reactivate, required=False)
    endpoints = forms.ModelMultipleChoiceField(Endpoint.objects, required=False, label="Systems / Endpoints")
    tags = TagField(required=False, help_text="Modify existing tags that help describe this scan.  "
                    "Choose from the list or add new tags. Press Enter key to add.")
    file = forms.FileField(widget=forms.widgets.FileInput(
        attrs={"accept": ".xml, .csv, .nessus, .json, .jsonl, .html, .js, .zip, .xlsx, .txt, .sarif"}),
        label="Choose report file",
        allow_empty_file=True,
        required=False)
    close_old_findings = forms.BooleanField(help_text="Select if old findings in the same test that are no longer present in the report get closed as mitigated when importing.",
                                            required=False, initial=True)
    version = forms.CharField(max_length=100, required=False, help_text="Version that will be set on existing Test object. Leave empty to leave existing value in place.")
    branch_tag = forms.CharField(max_length=100, required=False, help_text="Branch or Tag that was scanned.")
    commit_hash = forms.CharField(max_length=100, required=False, help_text="Commit that was scanned.")
    build_id = forms.CharField(max_length=100, required=False, help_text="ID of the build that was scanned.")
    api_scan_configuration = forms.ModelChoiceField(Product_API_Scan_Configuration.objects, required=False, label="API Scan Configuration")
    service = forms.CharField(max_length=200, required=False, help_text="A service is a self-contained piece of functionality within a Product. This is an optional field which is used in deduplication of findings when set.")
    source_code_management_uri = forms.URLField(max_length=600, required=False, help_text="Resource link to source code")
    apply_tags_to_findings = forms.BooleanField(
        help_text="If set to True, the tags will be applied to the findings",
        label="Apply Tags to Findings",
        required=False,
        initial=False,
    )
    apply_tags_to_endpoints = forms.BooleanField(
        help_text="If set to True, the tags will be applied to the endpoints",
        label="Apply Tags to Endpoints",
        required=False,
        initial=False,
    )

    if is_finding_groups_enabled():
        group_by = forms.ChoiceField(required=False, choices=Finding_Group.GROUP_BY_OPTIONS, help_text="Choose an option to automatically group new findings by the chosen option")
        create_finding_groups_for_all_findings = forms.BooleanField(help_text="If unchecked, finding groups will only be created when there is more than one grouped finding", required=False, initial=True)

    def __init__(self, *args, test=None, **kwargs):
        endpoints = kwargs.pop("endpoints", None)
        api_scan_configuration = kwargs.pop("api_scan_configuration", None)
        api_scan_configuration_queryset = kwargs.pop("api_scan_configuration_queryset", None)
        super().__init__(*args, **kwargs)
        self.fields["active"].initial = self.active_verified_choices[0]
        self.fields["verified"].initial = self.active_verified_choices[0]
        self.scan_type = None
        if test:
            self.scan_type = test.test_type.name
            self.fields["tags"].initial = test.tags.all()
        if endpoints:
            self.fields["endpoints"].queryset = endpoints
        if api_scan_configuration:
            self.initial["api_scan_configuration"] = api_scan_configuration
        if api_scan_configuration_queryset:
            self.fields["api_scan_configuration"].queryset = api_scan_configuration_queryset
        # couldn't find a cleaner way to add empty default
        if "group_by" in self.fields:
            choices = self.fields["group_by"].choices
            choices.insert(0, ("", "---------"))
            self.fields["group_by"].choices = choices

    def clean(self):
        cleaned_data = super().clean()
        file = cleaned_data.get("file")
        if requires_file(self.scan_type) and not file:
            msg = _("Uploading a report file is required for re-uploading findings.")
            raise forms.ValidationError(msg)
        if file and is_scan_file_too_large(file):
            msg = _("Report file is too large. Maximum supported size is %d MB") % settings.SCAN_FILE_MAX_SIZE
            raise forms.ValidationError(msg)
        tool_type = requires_tool_type(self.scan_type)
        if tool_type:
            api_scan_configuration = cleaned_data.get("api_scan_configuration")
            if api_scan_configuration and tool_type != api_scan_configuration.tool_configuration.tool_type.name:
                msg = f"API scan configuration must be of tool type {tool_type}"
                raise forms.ValidationError(msg)

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    # date can only be today or in the past, not the future
    def clean_scan_date(self):
        date = self.cleaned_data.get("scan_date", None)
        if date and date.date() > timezone.localtime(timezone.now()).date():
            msg = "The date cannot be in the future!"
            raise forms.ValidationError(msg)
        return date


class ImportEndpointMetaForm(forms.Form):
    file = forms.FileField(widget=forms.widgets.FileInput(
        attrs={"accept": ".csv"}),
        label="Choose meta file",
        required=True)  # Could not get required=True to actually accept the file as present
    create_endpoints = forms.BooleanField(
        label="Create nonexisting Endpoint",
        initial=True,
        required=False,
        help_text="Create endpoints that do not already exist")
    create_tags = forms.BooleanField(
        label="Add Tags",
        initial=True,
        required=False,
        help_text="Add meta from file as tags in the format key:value")
    create_dojo_meta = forms.BooleanField(
        label="Add Meta",
        initial=False,
        required=False,
        help_text="Add data from file as Metadata. Metadata is used for displaying custom fields")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)


class DoneForm(forms.Form):
    done = forms.BooleanField()


class UploadThreatForm(forms.Form):
    file = forms.FileField(widget=forms.widgets.FileInput(
        attrs={"accept": ".jpg,.png,.pdf"}),
        label="Select Threat Model")

    def clean(self):
        if (file := self.cleaned_data.get("file", None)) is not None:
            path = Path(file.name)
            ext = path.suffix
            valid_extensions = [".jpg", ".png", ".pdf"]
            if ext.lower() not in valid_extensions:
                if accepted_extensions := f"{', '.join(valid_extensions)}":
                    msg = (
                        "Unsupported extension. Supported extensions are as "
                        f"follows: {accepted_extensions}"
                    )
                else:
                    msg = (
                        "File uploads are prohibited due to the list of acceptable "
                        "file extensions being empty"
                    )
                raise ValidationError(msg)


class MergeFindings(forms.ModelForm):
    FINDING_ACTION = (("", "Select an Action"), ("inactive", "Inactive"), ("delete", "Delete"))

    append_description = forms.BooleanField(label="Append Description", initial=True, required=False,
                                            help_text="Description in all findings will be appended into the merged finding.")

    add_endpoints = forms.BooleanField(label="Add Endpoints", initial=True, required=False,
                                           help_text="Endpoints in all findings will be merged into the merged finding.")

    dynamic_raw = forms.BooleanField(label="Dynamic Scanner Raw Requests", initial=True, required=False,
                                           help_text="Dynamic scanner raw requests in all findings will be merged into the merged finding.")

    tag_finding = forms.BooleanField(label="Add Tags", initial=True, required=False,
                                           help_text="Tags in all findings will be merged into the merged finding.")

    mark_tag_finding = forms.BooleanField(label="Tag Merged Finding", initial=True, required=False,
                                           help_text="Creates a tag titled 'merged' for the finding that will be merged. If the 'Finding Action' is set to 'inactive' the inactive findings will be tagged with 'merged-inactive'.")

    append_reference = forms.BooleanField(label="Append Reference", initial=True, required=False,
                                            help_text="Reference in all findings will be appended into the merged finding.")

    finding_action = forms.ChoiceField(
        required=True,
        choices=FINDING_ACTION,
        label="Finding Action",
        help_text="The action to take on the merged finding. Set the findings to inactive or delete the findings.")

    def __init__(self, *args, **kwargs):
        _ = kwargs.pop("finding")
        findings = kwargs.pop("findings")
        super().__init__(*args, **kwargs)

        self.fields["finding_to_merge_into"] = forms.ModelChoiceField(
            queryset=findings, initial=0, required="False", label="Finding to Merge Into", help_text="Findings selected below will be merged into this finding.")

        # Exclude the finding to merge into from the findings to merge into
        self.fields["findings_to_merge"] = forms.ModelMultipleChoiceField(
            queryset=findings, required=True, label="Findings to Merge",
            widget=forms.widgets.SelectMultiple(attrs={"size": 10}),
            help_text=("Select the findings to merge."))
        self.field_order = ["finding_to_merge_into", "findings_to_merge", "append_description", "add_endpoints", "append_reference"]

    class Meta:
        model = Finding
        fields = ["append_description", "add_endpoints", "append_reference"]


class EditRiskAcceptanceForm(forms.ModelForm):
    # unfortunately django forces us to repeat many things here. choices, default, required etc.
    recommendation = forms.ChoiceField(choices=Risk_Acceptance.TREATMENT_CHOICES, initial=Risk_Acceptance.TREATMENT_ACCEPT, widget=forms.RadioSelect, label="Security Recommendation")
    decision = forms.ChoiceField(choices=Risk_Acceptance.TREATMENT_CHOICES, initial=Risk_Acceptance.TREATMENT_ACCEPT, widget=forms.RadioSelect)

    path = forms.FileField(label="Proof", required=False, widget=forms.widgets.FileInput(attrs={"accept": ".jpg,.png,.pdf"}))
    expiration_date = forms.DateTimeField(required=False, widget=forms.TextInput(attrs={"class": "datepicker"}))

    class Meta:
        model = Risk_Acceptance
        exclude = ["accepted_findings", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["path"].help_text = f"Existing proof uploaded: {self.instance.filename()}" if self.instance.filename() else "None"
        self.fields["expiration_date_warned"].disabled = True
        self.fields["expiration_date_handled"].disabled = True


class RiskAcceptanceForm(EditRiskAcceptanceForm):
    # path = forms.FileField(label="Proof", required=False, widget=forms.widgets.FileInput(attrs={"accept": ".jpg,.png,.pdf"}))
    # expiration_date = forms.DateTimeField(required=False, widget=forms.TextInput(attrs={'class': 'datepicker'}))
    accepted_findings = forms.ModelMultipleChoiceField(
        queryset=Finding.objects.none(), required=True,
        widget=forms.widgets.SelectMultiple(attrs={"size": 10}),
        help_text=("Active, verified findings listed, please select to add findings."))
    notes = forms.CharField(required=False, max_length=2400,
                            widget=forms.Textarea,
                            label="Notes")

    class Meta:
        model = Risk_Acceptance
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        expiration_delta_days = get_system_setting("risk_acceptance_form_default_days")
        logger.debug("expiration_delta_days: %i", expiration_delta_days)
        if expiration_delta_days > 0:
            expiration_date = timezone.now().date() + relativedelta(days=expiration_delta_days)
            # logger.debug('setting default expiration_date: %s', expiration_date)
            self.fields["expiration_date"].initial = expiration_date
        # self.fields['path'].help_text = 'Existing proof uploaded: %s' % self.instance.filename() if self.instance.filename() else 'None'
        self.fields["accepted_findings"].queryset = get_authorized_findings(Permissions.Risk_Acceptance)
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()


class BaseManageFileFormSet(forms.BaseModelFormSet):
    def clean(self):
        """Validate the IP/Mask combo is in CIDR format"""
        if any(self.errors):
            # Don't bother validating the formset unless each form is valid on its own
            return
        for form in self.forms:
            file = form.cleaned_data.get("file", None)
            if file:
                path = Path(file.name)
                ext = path.suffix
                valid_extensions = settings.FILE_UPLOAD_TYPES
                if ext.lower() not in valid_extensions:
                    if accepted_extensions := f"{', '.join(valid_extensions)}":
                        msg = (
                            "Unsupported extension. Supported extensions are as "
                            f"follows: {accepted_extensions}"
                        )
                    else:
                        msg = (
                            "File uploads are prohibited due to the list of acceptable "
                            "file extensions being empty"
                        )
                    form.add_error("file", msg)


ManageFileFormSet = modelformset_factory(FileUpload, extra=3, max_num=10, fields=["title", "file"], can_delete=True, formset=BaseManageFileFormSet)


class ReplaceRiskAcceptanceProofForm(forms.ModelForm):
    path = forms.FileField(label="Proof", required=True, widget=forms.widgets.FileInput(attrs={"accept": ".jpg,.png,.pdf"}))

    class Meta:
        model = Risk_Acceptance
        fields = ["path"]


class AddFindingsRiskAcceptanceForm(forms.ModelForm):

    accepted_findings = forms.ModelMultipleChoiceField(
        queryset=Finding.objects.none(),
        required=True,
        label="",
        widget=TableCheckboxWidget(attrs={"size": 25}),
    )

    class Meta:
        model = Risk_Acceptance
        fields = ["accepted_findings"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["accepted_findings"].queryset = get_authorized_findings(Permissions.Risk_Acceptance)


class CheckForm(forms.ModelForm):
    options = (("Pass", "Pass"), ("Fail", "Fail"), ("N/A", "N/A"))
    session_management = forms.ChoiceField(choices=options)
    encryption_crypto = forms.ChoiceField(choices=options)
    configuration_management = forms.ChoiceField(choices=options)
    authentication = forms.ChoiceField(choices=options)
    authorization_and_access_control = forms.ChoiceField(choices=options)
    data_input_sanitization_validation = forms.ChoiceField(choices=options)
    sensitive_data = forms.ChoiceField(choices=options)
    other = forms.ChoiceField(choices=options)

    def __init__(self, *args, **kwargs):
        findings = kwargs.pop("findings")
        super().__init__(*args, **kwargs)
        self.fields["session_issues"].queryset = findings
        self.fields["crypto_issues"].queryset = findings
        self.fields["config_issues"].queryset = findings
        self.fields["auth_issues"].queryset = findings
        self.fields["author_issues"].queryset = findings
        self.fields["data_issues"].queryset = findings
        self.fields["sensitive_issues"].queryset = findings
        self.fields["other_issues"].queryset = findings

    class Meta:
        model = Check_List
        fields = ["session_management", "session_issues", "encryption_crypto", "crypto_issues",
                  "configuration_management", "config_issues", "authentication", "auth_issues",
                  "authorization_and_access_control", "author_issues",
                  "data_input_sanitization_validation", "data_issues",
                  "sensitive_data", "sensitive_issues", "other", "other_issues"]


class EngForm(forms.ModelForm):
    name = forms.CharField(
        max_length=300, required=False,
        help_text=(
            "Add a descriptive name to identify this engagement. "
            "Without a name the target start date will be set."
        ))
    description = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=False, help_text="Description of the engagement and details regarding the engagement.")
    product = forms.ModelChoiceField(label="Product",
                                       queryset=Product.objects.none(),
                                       required=True)
    target_start = forms.DateField(widget=forms.TextInput(
        attrs={"class": "datepicker", "autocomplete": "off"}))
    target_end = forms.DateField(widget=forms.TextInput(
        attrs={"class": "datepicker", "autocomplete": "off"}))
    lead = forms.ModelChoiceField(
        queryset=None,
        required=True, label="Testing Lead")
    test_strategy = forms.URLField(required=False, label="Test Strategy URL")

    def __init__(self, *args, **kwargs):
        cicd = False
        product = None
        if "cicd" in kwargs:
            cicd = kwargs.pop("cicd")

        if "product" in kwargs:
            product = kwargs.pop("product")

        self.user = None
        if "user" in kwargs:
            self.user = kwargs.pop("user")

        super().__init__(*args, **kwargs)

        if product:
            self.fields["preset"] = forms.ModelChoiceField(help_text="Settings and notes for performing this engagement.", required=False, queryset=Engagement_Presets.objects.filter(product=product))
            self.fields["lead"].queryset = get_authorized_users_for_product_and_product_type(None, product, Permissions.Product_View).filter(is_active=True)
        else:
            self.fields["lead"].queryset = get_authorized_users(Permissions.Engagement_View).filter(is_active=True)

        self.fields["product"].queryset = get_authorized_products(Permissions.Engagement_Add)

        # Don't show CICD fields on a interactive engagement
        if cicd is False:
            del self.fields["build_id"]
            del self.fields["commit_hash"]
            del self.fields["branch_tag"]
            del self.fields["build_server"]
            del self.fields["source_code_management_server"]
            # del self.fields['source_code_management_uri']
            del self.fields["orchestration_engine"]
        else:
            del self.fields["test_strategy"]
            del self.fields["status"]

    def is_valid(self):
        valid = super().is_valid()

        # we're done now if not valid
        if not valid:
            return valid
        if self.cleaned_data["target_start"] > self.cleaned_data["target_end"]:
            self.add_error("target_start", "Your target start date exceeds your target end date")
            self.add_error("target_end", "Your target start date exceeds your target end date")
            return False
        return True

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    class Meta:
        model = Engagement
        exclude = ("first_contacted", "real_start", "engagement_type", "inherited_tags",
                   "real_end", "requester", "reason", "updated", "report_type",
                   "product", "threat_model", "api_test", "pen_test", "check_list")


class DeleteEngagementForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Engagement
        fields = ["id"]


class TestForm(forms.ModelForm):
    title = forms.CharField(max_length=255, required=False)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": "3"}), required=False)
    test_type = forms.ModelChoiceField(queryset=Test_Type.objects.all().order_by("name"))
    environment = forms.ModelChoiceField(
        queryset=Development_Environment.objects.all().order_by("name"))
    target_start = forms.DateTimeField(widget=forms.TextInput(
        attrs={"class": "datepicker", "autocomplete": "off"}))
    target_end = forms.DateTimeField(widget=forms.TextInput(
        attrs={"class": "datepicker", "autocomplete": "off"}))

    lead = forms.ModelChoiceField(
        queryset=None,
        required=False, label="Testing Lead")

    def __init__(self, *args, **kwargs):
        obj = None

        if "engagement" in kwargs:
            obj = kwargs.pop("engagement")

        if "instance" in kwargs:
            obj = kwargs.get("instance")

        super().__init__(*args, **kwargs)

        if obj:
            product = get_product(obj)
            self.fields["lead"].queryset = get_authorized_users_for_product_and_product_type(None, product, Permissions.Product_View).filter(is_active=True)
            self.fields["api_scan_configuration"].queryset = Product_API_Scan_Configuration.objects.filter(product=product)
        else:
            self.fields["lead"].queryset = get_authorized_users(Permissions.Test_View).filter(is_active=True)

    class Meta:
        model = Test
        fields = ["title", "test_type", "target_start", "target_end", "description",
                  "environment", "percent_complete", "tags", "lead", "version", "branch_tag", "build_id", "commit_hash",
                  "api_scan_configuration"]

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")


class DeleteTestForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Test
        fields = ["id"]


class CopyTestForm(forms.Form):
    engagement = forms.ModelChoiceField(
        required=True,
        queryset=Engagement.objects.none(),
        error_messages={"required": "*"})

    def __init__(self, *args, **kwargs):
        authorized_lists = kwargs.pop("engagements", None)
        super().__init__(*args, **kwargs)
        self.fields["engagement"].queryset = authorized_lists


class AddFindingForm(forms.ModelForm):
    title = forms.CharField(max_length=1000)
    date = forms.DateField(required=True,
                           widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    cwe = forms.IntegerField(required=False)
    vulnerability_ids = vulnerability_ids_field
    cvssv3 = forms.CharField(max_length=117, required=False, widget=forms.TextInput(attrs={"class": "cvsscalculator", "data-toggle": "dropdown", "aria-haspopup": "true", "aria-expanded": "false"}))
    description = forms.CharField(widget=forms.Textarea)
    severity = forms.ChoiceField(
        choices=SEVERITY_CHOICES,
        error_messages={
            "required": "Select valid choice: In Progress, On Hold, Completed",
            "invalid_choice": EFFORT_FOR_FIXING_INVALID_CHOICE})
    mitigation = forms.CharField(widget=forms.Textarea, required=False)
    impact = forms.CharField(widget=forms.Textarea, required=False)
    request = forms.CharField(widget=forms.Textarea, required=False)
    response = forms.CharField(widget=forms.Textarea, required=False)
    endpoints = forms.ModelMultipleChoiceField(Endpoint.objects.none(), required=False, label="Systems / Endpoints")
    endpoints_to_add = forms.CharField(max_length=5000, required=False, label="Endpoints to add",
                               help_text="The IP address, host name or full URL. You may enter one endpoint per line. "
                                         "Each must be valid.",
                               widget=forms.widgets.Textarea(attrs={"rows": "3", "cols": "400"}))
    references = forms.CharField(widget=forms.Textarea, required=False)
    publish_date = forms.DateField(widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}), required=False)
    planned_remediation_date = forms.DateField(widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}), required=False)
    planned_remediation_version = forms.CharField(max_length=99, required=False)
    effort_for_fixing = forms.ChoiceField(
        required=False,
        choices=EFFORT_FOR_FIXING_CHOICES,
        error_messages={
            "invalid_choice": EFFORT_FOR_FIXING_INVALID_CHOICE})

    # the only reliable way without hacking internal fields to get predicatble ordering is to make it explicit
    field_order = ("title", "date", "cwe", "vulnerability_ids", "severity", "cvssv3", "description", "mitigation", "impact", "request", "response", "steps_to_reproduce",
                   "severity_justification", "endpoints", "endpoints_to_add", "references", "active", "verified", "false_p", "duplicate", "out_of_scope",
                   "risk_accepted", "under_defect_review")

    def __init__(self, *args, **kwargs):
        req_resp = kwargs.pop("req_resp")

        product = None
        if "product" in kwargs:
            product = kwargs.pop("product")

        super().__init__(*args, **kwargs)

        if product:
            self.fields["endpoints"].queryset = Endpoint.objects.filter(product=product)

        if req_resp:
            self.fields["request"].initial = req_resp[0]
            self.fields["response"].initial = req_resp[1]

        self.endpoints_to_add_list = []

    def clean(self):
        cleaned_data = super().clean()
        if ((cleaned_data["active"] or cleaned_data["verified"]) and cleaned_data["duplicate"]):
            msg = "Duplicate findings cannot be verified or active"
            raise forms.ValidationError(msg)
        if cleaned_data["false_p"] and cleaned_data["verified"]:
            msg = "False positive findings cannot be verified."
            raise forms.ValidationError(msg)
        if cleaned_data["active"] and "risk_accepted" in cleaned_data and cleaned_data["risk_accepted"]:
            msg = "Active findings cannot be risk accepted."
            raise forms.ValidationError(msg)

        endpoints_to_add_list, errors = validate_endpoints_to_add(cleaned_data["endpoints_to_add"])
        if errors:
            raise forms.ValidationError(errors)
        self.endpoints_to_add_list = endpoints_to_add_list

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    class Meta:
        model = Finding
        exclude = ("reporter", "url", "numerical_severity", "under_review", "reviewers", "cve", "inherited_tags",
                   "review_requested_by", "is_mitigated", "jira_creation", "jira_change", "endpoints", "sla_start_date")


class AdHocFindingForm(forms.ModelForm):
    title = forms.CharField(max_length=1000)
    date = forms.DateField(required=True,
                           widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    cwe = forms.IntegerField(required=False)
    vulnerability_ids = vulnerability_ids_field
    cvssv3 = forms.CharField(max_length=117, required=False, widget=forms.TextInput(attrs={"class": "cvsscalculator", "data-toggle": "dropdown", "aria-haspopup": "true", "aria-expanded": "false"}))
    description = forms.CharField(widget=forms.Textarea)
    severity = forms.ChoiceField(
        choices=SEVERITY_CHOICES,
        error_messages={
            "required": "Select valid choice: In Progress, On Hold, Completed",
            "invalid_choice": EFFORT_FOR_FIXING_INVALID_CHOICE})
    mitigation = forms.CharField(widget=forms.Textarea, required=False)
    impact = forms.CharField(widget=forms.Textarea, required=False)
    request = forms.CharField(widget=forms.Textarea, required=False)
    response = forms.CharField(widget=forms.Textarea, required=False)
    endpoints = forms.ModelMultipleChoiceField(queryset=Endpoint.objects.none(), required=False, label="Systems / Endpoints")
    endpoints_to_add = forms.CharField(max_length=5000, required=False, label="Endpoints to add",
                               help_text="The IP address, host name or full URL. You may enter one endpoint per line. "
                                         "Each must be valid.",
                               widget=forms.widgets.Textarea(attrs={"rows": "3", "cols": "400"}))
    references = forms.CharField(widget=forms.Textarea, required=False)
    publish_date = forms.DateField(widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}), required=False)
    planned_remediation_date = forms.DateField(widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}), required=False)
    planned_remediation_version = forms.CharField(max_length=99, required=False)
    effort_for_fixing = forms.ChoiceField(
        required=False,
        choices=EFFORT_FOR_FIXING_CHOICES,
        error_messages={
            "invalid_choice": EFFORT_FOR_FIXING_INVALID_CHOICE})

    # the only reliable way without hacking internal fields to get predicatble ordering is to make it explicit
    field_order = ("title", "date", "cwe", "vulnerability_ids", "severity", "cvssv3", "description", "mitigation", "impact", "request", "response", "steps_to_reproduce",
                   "severity_justification", "endpoints", "endpoints_to_add", "references", "active", "verified", "false_p", "duplicate", "out_of_scope",
                   "risk_accepted", "under_defect_review", "sla_start_date", "sla_expiration_date")

    def __init__(self, *args, **kwargs):
        req_resp = kwargs.pop("req_resp")

        product = None
        if "product" in kwargs:
            product = kwargs.pop("product")

        super().__init__(*args, **kwargs)

        if product:
            self.fields["endpoints"].queryset = Endpoint.objects.filter(product=product)

        if req_resp:
            self.fields["request"].initial = req_resp[0]
            self.fields["response"].initial = req_resp[1]

        self.endpoints_to_add_list = []

    def clean(self):
        cleaned_data = super().clean()
        if ((cleaned_data["active"] or cleaned_data["verified"]) and cleaned_data["duplicate"]):
            msg = "Duplicate findings cannot be verified or active"
            raise forms.ValidationError(msg)
        if cleaned_data["false_p"] and cleaned_data["verified"]:
            msg = "False positive findings cannot be verified."
            raise forms.ValidationError(msg)

        endpoints_to_add_list, errors = validate_endpoints_to_add(cleaned_data["endpoints_to_add"])
        if errors:
            raise forms.ValidationError(errors)
        self.endpoints_to_add_list = endpoints_to_add_list

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    class Meta:
        model = Finding
        exclude = ("reporter", "url", "numerical_severity", "under_review", "reviewers", "cve", "inherited_tags",
                   "review_requested_by", "is_mitigated", "jira_creation", "jira_change", "endpoints", "sla_start_date",
                   "sla_expiration_date")


class PromoteFindingForm(forms.ModelForm):
    title = forms.CharField(max_length=1000)
    date = forms.DateField(required=True,
                           widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    cwe = forms.IntegerField(required=False)
    vulnerability_ids = vulnerability_ids_field
    cvssv3 = forms.CharField(max_length=117, required=False, widget=forms.TextInput(attrs={"class": "cvsscalculator", "data-toggle": "dropdown", "aria-haspopup": "true", "aria-expanded": "false"}))
    description = forms.CharField(widget=forms.Textarea)
    severity = forms.ChoiceField(
        choices=SEVERITY_CHOICES,
        error_messages={
            "required": "Select valid choice: In Progress, On Hold, Completed",
            "invalid_choice": "Select valid choice: Critical,High,Medium,Low"})
    mitigation = forms.CharField(widget=forms.Textarea, required=False)
    impact = forms.CharField(widget=forms.Textarea, required=False)
    endpoints = forms.ModelMultipleChoiceField(Endpoint.objects.none(), required=False, label="Systems / Endpoints")
    endpoints_to_add = forms.CharField(max_length=5000, required=False, label="Endpoints to add",
                               help_text="The IP address, host name or full URL. You may enter one endpoint per line. "
                                         "Each must be valid.",
                               widget=forms.widgets.Textarea(attrs={"rows": "3", "cols": "400"}))
    references = forms.CharField(widget=forms.Textarea, required=False)

    # the onyl reliable way without hacking internal fields to get predicatble ordering is to make it explicit
    field_order = ("title", "group", "date", "sla_start_date", "sla_expiration_date", "cwe", "vulnerability_ids", "severity", "cvssv3",
                   "cvssv3_score", "description", "mitigation", "impact", "request", "response", "steps_to_reproduce", "severity_justification",
                   "endpoints", "endpoints_to_add", "references", "active", "mitigated", "mitigated_by", "verified", "false_p", "duplicate",
                   "out_of_scope", "risk_accept", "under_defect_review")

    def __init__(self, *args, **kwargs):
        product = None
        if "product" in kwargs:
            product = kwargs.pop("product")

        super().__init__(*args, **kwargs)

        if product:
            self.fields["endpoints"].queryset = Endpoint.objects.filter(product=product)

        self.endpoints_to_add_list = []

    def clean(self):
        cleaned_data = super().clean()

        endpoints_to_add_list, errors = validate_endpoints_to_add(cleaned_data["endpoints_to_add"])
        if errors:
            raise forms.ValidationError(errors)
        self.endpoints_to_add_list = endpoints_to_add_list

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    class Meta:
        model = Finding
        exclude = ("reporter", "url", "numerical_severity", "active", "false_p", "verified", "endpoint_status", "cve", "inherited_tags",
                   "duplicate", "out_of_scope", "under_review", "reviewers", "review_requested_by", "is_mitigated", "jira_creation", "jira_change", "planned_remediation_date", "planned_remediation_version", "effort_for_fixing")


class FindingForm(forms.ModelForm):
    title = forms.CharField(max_length=1000)
    group = forms.ModelChoiceField(required=False, queryset=Finding_Group.objects.none(), help_text="The Finding Group to which this finding belongs, leave empty to remove the finding from the group. Groups can only be created via Bulk Edit for now.")
    date = forms.DateField(required=True,
                           widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    cwe = forms.IntegerField(required=False)
    vulnerability_ids = vulnerability_ids_field
    cvssv3 = forms.CharField(max_length=117, required=False, widget=forms.TextInput(attrs={"class": "cvsscalculator", "data-toggle": "dropdown", "aria-haspopup": "true", "aria-expanded": "false"}))
    cvssv3_score = forms.FloatField(required=False, max_value=10.0, min_value=0.0)
    description = forms.CharField(widget=forms.Textarea)
    severity = forms.ChoiceField(
        choices=SEVERITY_CHOICES,
        error_messages={
            "required": "Select valid choice: In Progress, On Hold, Completed",
            "invalid_choice": "Select valid choice: Critical,High,Medium,Low"})
    mitigation = forms.CharField(widget=forms.Textarea, required=False)
    impact = forms.CharField(widget=forms.Textarea, required=False)
    request = forms.CharField(widget=forms.Textarea, required=False)
    response = forms.CharField(widget=forms.Textarea, required=False)
    endpoints = forms.ModelMultipleChoiceField(queryset=Endpoint.objects.none(), required=False, label="Systems / Endpoints")
    endpoints_to_add = forms.CharField(max_length=5000, required=False, label="Endpoints to add",
                               help_text="The IP address, host name or full URL. You may enter one endpoint per line. "
                                         "Each must be valid.",
                               widget=forms.widgets.Textarea(attrs={"rows": "3", "cols": "400"}))
    references = forms.CharField(widget=forms.Textarea, required=False)

    mitigated = forms.DateField(required=False, help_text="Date and time when the flaw has been fixed", widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    mitigated_by = forms.ModelChoiceField(required=False, queryset=Dojo_User.objects.none())

    publish_date = forms.DateField(widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}), required=False)
    planned_remediation_date = forms.DateField(widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}), required=False)
    planned_remediation_version = forms.CharField(max_length=99, required=False)
    effort_for_fixing = forms.ChoiceField(
        required=False,
        choices=EFFORT_FOR_FIXING_CHOICES,
        error_messages={
            "invalid_choice": EFFORT_FOR_FIXING_INVALID_CHOICE})

    # the only reliable way without hacking internal fields to get predicatble ordering is to make it explicit
    field_order = ("title", "group", "date", "sla_start_date", "sla_expiration_date", "cwe", "vulnerability_ids", "severity", "cvssv3",
                   "cvssv3_score", "description", "mitigation", "impact", "request", "response", "steps_to_reproduce", "severity_justification",
                   "endpoints", "endpoints_to_add", "references", "active", "mitigated", "mitigated_by", "verified", "false_p", "duplicate",
                   "out_of_scope", "risk_accept", "under_defect_review")

    def __init__(self, *args, **kwargs):
        req_resp = None
        if "req_resp" in kwargs:
            req_resp = kwargs.pop("req_resp")

        self.can_edit_mitigated_data = kwargs.pop("can_edit_mitigated_data") if "can_edit_mitigated_data" in kwargs \
            else False

        super().__init__(*args, **kwargs)

        self.fields["endpoints"].queryset = Endpoint.objects.filter(product=self.instance.test.engagement.product)
        self.fields["mitigated_by"].queryset = get_authorized_users(Permissions.Test_Edit)

        # do not show checkbox if finding is not accepted and simple risk acceptance is disabled
        # if checked, always show to allow unaccept also with full risk acceptance enabled
        # when adding from template, we don't have access to the test. quickfix for now to just hide simple risk acceptance
        if not hasattr(self.instance, "test") or (not self.instance.risk_accepted and not self.instance.test.engagement.product.enable_simple_risk_acceptance):
            del self.fields["risk_accepted"]
        elif self.instance.risk_accepted:
            self.fields["risk_accepted"].help_text = "Uncheck to unaccept the risk. Use full risk acceptance from the dropdown menu if you need advanced settings such as an expiry date."
        elif self.instance.test.engagement.product.enable_simple_risk_acceptance:
            self.fields["risk_accepted"].help_text = "Check to accept the risk. Use full risk acceptance from the dropdown menu if you need advanced settings such as an expiry date."

        # self.fields['tags'].widget.choices = t
        if req_resp:
            self.fields["request"].initial = req_resp[0]
            self.fields["response"].initial = req_resp[1]

        if self.instance.duplicate:
            self.fields["duplicate"].help_text = "Original finding that is being duplicated here (readonly). Use view finding page to manage duplicate relationships. Unchecking duplicate here will reset this findings duplicate status, but will trigger deduplication logic."
        else:
            self.fields["duplicate"].help_text = "You can mark findings as duplicate only from the view finding page."

        self.fields["sla_start_date"].disabled = True
        self.fields["sla_expiration_date"].disabled = True

        if self.can_edit_mitigated_data:
            if hasattr(self, "instance"):
                self.fields["mitigated"].initial = self.instance.mitigated
                self.fields["mitigated_by"].initial = self.instance.mitigated_by
        else:
            del self.fields["mitigated"]
            del self.fields["mitigated_by"]

        if not is_finding_groups_enabled() or not hasattr(self.instance, "test"):
            del self.fields["group"]
        else:
            self.fields["group"].queryset = self.instance.test.finding_group_set.all()
            self.fields["group"].initial = self.instance.finding_group

        self.endpoints_to_add_list = []

    def clean(self):
        cleaned_data = super().clean()

        if (cleaned_data["active"] or cleaned_data["verified"]) and cleaned_data["duplicate"]:
            msg = "Duplicate findings cannot be verified or active"
            raise forms.ValidationError(msg)
        if cleaned_data["false_p"] and cleaned_data["verified"]:
            msg = "False positive findings cannot be verified."
            raise forms.ValidationError(msg)
        if cleaned_data["active"] and "risk_accepted" in cleaned_data and cleaned_data["risk_accepted"]:
            msg = "Active findings cannot be risk accepted."
            raise forms.ValidationError(msg)

        endpoints_to_add_list, errors = validate_endpoints_to_add(cleaned_data["endpoints_to_add"])
        if errors:
            raise forms.ValidationError(errors)
        self.endpoints_to_add_list = endpoints_to_add_list

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    def _post_clean(self):
        super()._post_clean()

        if self.can_edit_mitigated_data:
            opts = self.instance._meta
            try:
                opts.get_field("mitigated").save_form_data(self.instance, self.cleaned_data.get("mitigated"))
                opts.get_field("mitigated_by").save_form_data(self.instance, self.cleaned_data.get("mitigated_by"))
            except forms.ValidationError as e:
                self._update_errors(e)

    class Meta:
        model = Finding
        exclude = ("reporter", "url", "numerical_severity", "under_review", "reviewers", "cve", "inherited_tags",
                   "review_requested_by", "is_mitigated", "jira_creation", "jira_change", "sonarqube_issue", "endpoint_status")


class StubFindingForm(forms.ModelForm):
    title = forms.CharField(required=True, max_length=1000)

    class Meta:
        model = Stub_Finding
        order = ("title",)
        exclude = (
            "date", "description", "severity", "reporter", "test", "is_mitigated")

    def clean(self):
        cleaned_data = super().clean()
        if "title" in cleaned_data:
            if len(cleaned_data["title"]) <= 0:
                msg = "The title is required."
                raise forms.ValidationError(msg)
        else:
            msg = "The title is required."
            raise forms.ValidationError(msg)

        return cleaned_data


class ApplyFindingTemplateForm(forms.Form):

    title = forms.CharField(max_length=1000, required=True)

    cwe = forms.IntegerField(label="CWE", required=False)
    vulnerability_ids = vulnerability_ids_field
    cvssv3 = forms.CharField(label="CVSSv3", max_length=117, required=False, widget=forms.TextInput(attrs={"class": "btn btn-secondary dropdown-toggle", "data-toggle": "dropdown", "aria-haspopup": "true", "aria-expanded": "false"}))

    severity = forms.ChoiceField(required=False, choices=SEVERITY_CHOICES, error_messages={"required": "Select valid choice: In Progress, On Hold, Completed", "invalid_choice": "Select valid choice: Critical,High,Medium,Low"})

    description = forms.CharField(widget=forms.Textarea)
    mitigation = forms.CharField(widget=forms.Textarea, required=False)
    impact = forms.CharField(widget=forms.Textarea, required=False)
    references = forms.CharField(widget=forms.Textarea, required=False)

    tags = TagField(required=False, help_text="Add tags that help describe this finding template. Choose from the list or add new tags. Press Enter key to add.", initial=Finding.tags.tag_model.objects.all().order_by("name"))

    def __init__(self, template=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tags"].autocomplete_tags = Finding.tags.tag_model.objects.all().order_by("name")
        self.template = template
        if template:
            self.template.vulnerability_ids = "\n".join(template.vulnerability_ids)

    def clean(self):
        cleaned_data = super().clean()

        if "title" in cleaned_data:
            if len(cleaned_data["title"]) <= 0:
                msg = "The title is required."
                raise forms.ValidationError(msg)
        else:
            msg = "The title is required."
            raise forms.ValidationError(msg)

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    class Meta:
        fields = ["title", "cwe", "vulnerability_ids", "cvssv3", "severity", "description", "mitigation", "impact", "references", "tags"]
        order = ("title", "cwe", "vulnerability_ids", "cvssv3", "severity", "description", "impact", "is_mitigated")


class FindingTemplateForm(forms.ModelForm):
    apply_to_findings = forms.BooleanField(required=False, help_text="Apply template to all findings that match this CWE. (Update will overwrite mitigation, impact and references for any active, verified findings.)")
    title = forms.CharField(max_length=1000, required=True)

    cwe = forms.IntegerField(label="CWE", required=False)
    vulnerability_ids = vulnerability_ids_field
    cvssv3 = forms.CharField(max_length=117, required=False, widget=forms.TextInput(attrs={"class": "btn btn-secondary dropdown-toggle", "data-toggle": "dropdown", "aria-haspopup": "true", "aria-expanded": "false"}))
    severity = forms.ChoiceField(
        required=False,
        choices=SEVERITY_CHOICES,
        error_messages={
            "required": "Select valid choice: In Progress, On Hold, Completed",
            "invalid_choice": "Select valid choice: Critical,High,Medium,Low"})

    field_order = ["title", "cwe", "vulnerability_ids", "severity", "cvssv3", "description", "mitigation", "impact", "references", "tags", "template_match", "template_match_cwe", "template_match_title", "apply_to_findings"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tags"].autocomplete_tags = Finding.tags.tag_model.objects.all().order_by("name")

    class Meta:
        model = Finding_Template
        order = ("title", "cwe", "vulnerability_ids", "cvssv3", "severity", "description", "impact")
        exclude = ("numerical_severity", "is_mitigated", "last_used", "endpoint_status", "cve")

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")


class DeleteFindingTemplateForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Finding_Template
        fields = ["id"]


class FindingBulkUpdateForm(forms.ModelForm):
    status = forms.BooleanField(required=False)
    risk_acceptance = forms.BooleanField(required=False)
    risk_accept = forms.BooleanField(required=False)
    risk_unaccept = forms.BooleanField(required=False)

    date = forms.DateField(required=False, widget=forms.DateInput(attrs={"class": "datepicker"}))
    planned_remediation_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"class": "datepicker"}))
    planned_remediation_version = forms.CharField(required=False, max_length=99, widget=forms.TextInput(attrs={"class": "form-control"}))
    finding_group = forms.BooleanField(required=False)
    finding_group_create = forms.BooleanField(required=False)
    finding_group_create_name = forms.CharField(required=False)
    finding_group_add = forms.BooleanField(required=False)
    add_to_finding_group_id = forms.CharField(required=False)
    finding_group_remove = forms.BooleanField(required=False)
    finding_group_by = forms.BooleanField(required=False)
    finding_group_by_option = forms.CharField(required=False)

    push_to_jira = forms.BooleanField(required=False)
    # unlink_from_jira = forms.BooleanField(required=False)
    push_to_github = forms.BooleanField(required=False)
    tags = TagField(required=False, autocomplete_tags=Finding.tags.tag_model.objects.all().order_by("name"))
    notes = forms.CharField(required=False, max_length=1024, widget=forms.TextInput(attrs={"class": "form-control"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["severity"].required = False
        # we need to defer initialization to prevent multiple initializations if other forms are shown
        self.fields["tags"].widget.tag_options = tagulous.models.options.TagOptions(autocomplete_settings={"width": "200px", "defer": True})
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()

    def clean(self):
        cleaned_data = super().clean()

        if (cleaned_data["active"] or cleaned_data["verified"]) and cleaned_data["duplicate"]:
            msg = "Duplicate findings cannot be verified or active"
            raise forms.ValidationError(msg)
        if cleaned_data["false_p"] and cleaned_data["verified"]:
            msg = "False positive findings cannot be verified."
            raise forms.ValidationError(msg)
        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")

    class Meta:
        model = Finding
        fields = ("severity", "date", "planned_remediation_date", "active", "verified", "false_p", "duplicate", "out_of_scope",
                  "is_mitigated")


class EditEndpointForm(forms.ModelForm):
    class Meta:
        model = Endpoint
        exclude = ["product", "inherited_tags"]

    def __init__(self, *args, **kwargs):
        self.product = None
        self.endpoint_instance = None
        super().__init__(*args, **kwargs)
        if "instance" in kwargs:
            self.endpoint_instance = kwargs.pop("instance")
            self.product = self.endpoint_instance.product
            product_id = self.endpoint_instance.product.pk
            findings = Finding.objects.filter(test__engagement__product__id=product_id)
            self.fields["findings"].queryset = findings

    def clean(self):

        cleaned_data = super().clean()

        protocol = cleaned_data["protocol"]
        userinfo = cleaned_data["userinfo"]
        host = cleaned_data["host"]
        port = cleaned_data["port"]
        path = cleaned_data["path"]
        query = cleaned_data["query"]
        fragment = cleaned_data["fragment"]

        endpoint = endpoint_filter(
            protocol=protocol,
            userinfo=userinfo,
            host=host,
            port=port,
            path=path,
            query=query,
            fragment=fragment,
            product=self.product,
        )
        if endpoint.count() > 1 or (endpoint.count() == 1 and endpoint.first().pk != self.endpoint_instance.pk):
            msg = "It appears as though an endpoint with this data already exists for this product."
            raise forms.ValidationError(msg, code="invalid")

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")


class AddEndpointForm(forms.Form):
    endpoint = forms.CharField(max_length=5000, required=True, label="Endpoint(s)",
                               help_text="The IP address, host name or full URL. You may enter one endpoint per line. "
                                         "Each must be valid.",
                               widget=forms.widgets.Textarea(attrs={"rows": "15", "cols": "400"}))
    product = forms.CharField(required=True,
                              widget=forms.widgets.HiddenInput(), help_text="The product this endpoint should be "
                                                                            "associated with.")
    tags = TagField(required=False,
                    help_text="Add tags that help describe this endpoint.  "
                              "Choose from the list or add new tags. Press Enter key to add.")

    def __init__(self, *args, **kwargs):
        product = None
        if "product" in kwargs:
            product = kwargs.pop("product")
        super().__init__(*args, **kwargs)
        self.fields["product"] = forms.ModelChoiceField(queryset=get_authorized_products(Permissions.Endpoint_Add))
        if product is not None:
            self.fields["product"].initial = product.id

        self.product = product
        self.endpoints_to_process = []

    def save(self):
        processed_endpoints = []
        for e in self.endpoints_to_process:
            endpoint, _created = endpoint_get_or_create(
                protocol=e[0],
                userinfo=e[1],
                host=e[2],
                port=e[3],
                path=e[4],
                query=e[5],
                fragment=e[6],
                product=self.product,
            )
            processed_endpoints.append(endpoint)
        return processed_endpoints

    def clean(self):

        cleaned_data = super().clean()

        if "endpoint" in cleaned_data and "product" in cleaned_data:
            endpoint = cleaned_data["endpoint"]
            product = cleaned_data["product"]
            if isinstance(product, Product):
                self.product = product
            else:
                self.product = Product.objects.get(id=int(product))
        else:
            msg = "Please enter a valid URL or IP address."
            raise forms.ValidationError(msg, code="invalid")

        endpoints_to_add_list, errors = validate_endpoints_to_add(endpoint)
        if errors:
            raise forms.ValidationError(errors)
        self.endpoints_to_process = endpoints_to_add_list

        return cleaned_data

    def clean_tags(self):
        tag_validator(self.cleaned_data.get("tags"))
        return self.cleaned_data.get("tags")


class DeleteEndpointForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Endpoint
        fields = ["id"]


class NoteForm(forms.ModelForm):
    entry = forms.CharField(max_length=2400, widget=forms.Textarea(attrs={"rows": 4, "cols": 15}),
                            label="Notes:")

    class Meta:
        model = Notes
        fields = ["entry", "private"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()


class TypedNoteForm(NoteForm):

    def __init__(self, *args, **kwargs):
        queryset = kwargs.pop("available_note_types")
        super().__init__(*args, **kwargs)
        self.fields["note_type"] = forms.ModelChoiceField(queryset=queryset, label="Note Type", required=True)

    class Meta:
        model = Notes
        fields = ["note_type", "entry", "private"]


class DeleteNoteForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Notes
        fields = ["id"]


class CloseFindingForm(forms.ModelForm):
    entry = forms.CharField(
        required=True, max_length=2400,
        widget=forms.Textarea, label="Notes:",
        error_messages={"required": ("The reason for closing a finding is "
                                     "required, please use the text area "
                                     "below to provide documentation.")})

    mitigated = forms.DateField(required=False, help_text="Date and time when the flaw has been fixed", widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    mitigated_by = forms.ModelChoiceField(required=False, queryset=Dojo_User.objects.none())
    false_p = forms.BooleanField(initial=False, required=False, label="False Positive")
    out_of_scope = forms.BooleanField(initial=False, required=False, label="Out of Scope")
    duplicate = forms.BooleanField(initial=False, required=False, label="Duplicate")

    def __init__(self, *args, **kwargs):
        queryset = kwargs.pop("missing_note_types")
        super().__init__(*args, **kwargs)
        if len(queryset) == 0:
            self.fields["note_type"].widget = forms.HiddenInput()
        else:
            self.fields["note_type"] = forms.ModelChoiceField(queryset=queryset, label="Note Type", required=True)

        self.can_edit_mitigated_data = kwargs.pop("can_edit_mitigated_data") if "can_edit_mitigated_data" in kwargs \
            else False

        if self.can_edit_mitigated_data:
            self.fields["mitigated_by"].queryset = get_authorized_users(Permissions.Test_Edit)
            self.fields["mitigated"].initial = self.instance.mitigated
            self.fields["mitigated_by"].initial = self.instance.mitigated_by
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()

    def _post_clean(self):
        super()._post_clean()

        if self.can_edit_mitigated_data:
            opts = self.instance._meta
            if not self.cleaned_data.get("active"):
                try:
                    opts.get_field("mitigated").save_form_data(self.instance, self.cleaned_data.get("mitigated"))
                    opts.get_field("mitigated_by").save_form_data(self.instance, self.cleaned_data.get("mitigated_by"))
                except forms.ValidationError as e:
                    self._update_errors(e)

    class Meta:
        model = Notes
        fields = ["note_type", "entry", "mitigated", "mitigated_by", "false_p", "out_of_scope", "duplicate"]


class EditPlannedRemediationDateFindingForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        finding = None
        if "finding" in kwargs:
            finding = kwargs.pop("finding")

        super().__init__(*args, **kwargs)

        self.fields["planned_remediation_date"].required = True
        self.fields["planned_remediation_date"].widget = forms.DateInput(attrs={"class": "datepicker"})

        if finding is not None:
            self.fields["planned_remediation_date"].initial = finding.planned_remediation_date

    class Meta:
        model = Finding
        fields = ["planned_remediation_date"]


class DefectFindingForm(forms.ModelForm):
    CLOSE_CHOICES = (("Close Finding", "Close Finding"), ("Not Fixed", "Not Fixed"))
    defect_choice = forms.ChoiceField(required=True, choices=CLOSE_CHOICES)

    entry = forms.CharField(
        required=True, max_length=2400,
        widget=forms.Textarea, label="Notes:",
        error_messages={"required": ("The reason for closing a finding is "
                                     "required, please use the text area "
                                     "below to provide documentation.")})

    class Meta:
        model = Notes
        fields = ["entry"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()


class ClearFindingReviewForm(forms.ModelForm):
    entry = forms.CharField(
        required=True, max_length=2400,
        help_text="Please provide a message.",
        widget=forms.Textarea, label="Notes:",
        error_messages={"required": ("The reason for clearing a review is "
                                     "required, please use the text area "
                                     "below to provide documentation.")})

    class Meta:
        model = Finding
        fields = ["active", "verified", "false_p", "out_of_scope", "duplicate", "is_mitigated"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()


class ReviewFindingForm(forms.Form):
    reviewers = forms.MultipleChoiceField(
        help_text=(
            "Select all users who can review Finding. Only users with "
            "at least write permission to this finding can be selected"),
        required=False,
    )
    entry = forms.CharField(
        required=True, max_length=2400,
        help_text="Please provide a message for reviewers.",
        widget=forms.Textarea, label="Notes:",
        error_messages={"required": ("The reason for requesting a review is "
                                     "required, please use the text area "
                                     "below to provide documentation.")})
    allow_all_reviewers = forms.BooleanField(
        required=False,
        label="Allow All Eligible Reviewers",
        help_text=("Checking this box will allow any user in the drop down "
                   "above to provide a review for this finding"))

    def __init__(self, *args, **kwargs):
        finding = kwargs.pop("finding", None)
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        # Get the list of users
        if finding is not None:
            users = get_authorized_users_for_product_and_product_type(None, finding.test.engagement.product, Permissions.Finding_Edit)
        else:
            users = get_authorized_users(Permissions.Finding_Edit).filter(is_active=True)
        # Remove the current user
        if user is not None:
            users = users.exclude(id=user.id)
        # Save a copy of the original query to be used in the validator
        self.reviewer_queryset = users
        # Set the users in the form
        self.fields["reviewers"].choices = self._get_choices(self.reviewer_queryset)
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()

    @staticmethod
    def _get_choices(queryset):
        return [(item.pk, item.get_full_name()) for item in queryset]

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("allow_all_reviewers", False):
            cleaned_data["reviewers"] = [user.id for user in self.reviewer_queryset]
        if len(cleaned_data.get("reviewers", [])) == 0:
            msg = "Please select at least one user from the reviewers list"
            raise ValidationError(msg)
        return cleaned_data

    class Meta:
        fields = ["reviewers", "entry", "allow_all_reviewers"]


class WeeklyMetricsForm(forms.Form):
    dates = forms.ChoiceField()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        wmf_options = []

        for i in range(6):
            # Weeks start on Monday
            curr = datetime.now() - relativedelta(weeks=i)
            start_of_period = curr - relativedelta(weeks=1, weekday=0,
                                                   hour=0, minute=0, second=0)
            end_of_period = curr + relativedelta(weeks=0, weekday=0,
                                                 hour=0, minute=0, second=0)

            wmf_options.append((end_of_period.strftime("%b %d %Y %H %M %S %Z"),
                                start_of_period.strftime("%b %d")
                                + " - " + end_of_period.strftime("%b %d")))

        wmf_options = tuple(wmf_options)

        self.fields["dates"].choices = wmf_options


class SimpleMetricsForm(forms.Form):
    date = forms.DateField(
        label="",
        widget=MonthYearWidget())


class SimpleSearchForm(forms.Form):
    query = forms.CharField(required=False)


class DateRangeMetrics(forms.Form):
    start_date = forms.DateField(required=True, label="To",
                                 widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    end_date = forms.DateField(required=True,
                               label="From",
                               widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))


class MetricsFilterForm(forms.Form):
    start_date = forms.DateField(required=False,
                                 label="To",
                                 widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    end_date = forms.DateField(required=False,
                               label="From",
                               widget=forms.TextInput(attrs={"class": "datepicker", "autocomplete": "off"}))
    finding_status = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=FINDING_STATUS,
        label="Status")
    severity = forms.MultipleChoiceField(required=False,
                                         choices=(("Low", "Low"),
                                                  ("Medium", "Medium"),
                                                  ("High", "High"),
                                                  ("Critical", "Critical")),
                                         help_text=('Hold down "Control", or '
                                                    '"Command" on a Mac, to '
                                                    'select more than one.'))
    exclude_product_types = forms.ModelMultipleChoiceField(
        required=False, queryset=Product_Type.objects.all().order_by("name"))

    # add the ability to exclude the exclude_product_types field
    def __init__(self, *args, **kwargs):
        exclude_product_types = kwargs.pop("exclude_product_types", False)
        super().__init__(*args, **kwargs)
        if exclude_product_types:
            del self.fields["exclude_product_types"]


class DojoGroupForm(forms.ModelForm):

    name = forms.CharField(max_length=255, required=True)
    description = forms.CharField(widget=forms.Textarea(attrs={}), required=False)

    class Meta:
        model = Dojo_Group
        fields = ["name", "description"]
        exclude = ["users"]


class DeleteGroupForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Dojo_Group
        fields = ["id"]


class Add_Group_MemberForm(forms.ModelForm):
    users = forms.ModelMultipleChoiceField(queryset=Dojo_Group_Member.objects.none(), required=True, label="Users")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].disabled = True
        current_members = Dojo_Group_Member.objects.filter(group=self.initial["group"]).values_list("user", flat=True)
        self.fields["users"].queryset = Dojo_User.objects.exclude(
            Q(is_superuser=True)
            | Q(id__in=current_members)).exclude(is_active=False).order_by("first_name", "last_name")
        self.fields["role"].queryset = get_group_member_roles()

    class Meta:
        model = Dojo_Group_Member
        fields = ["group", "users", "role"]


class Add_Group_Member_UserForm(forms.ModelForm):
    groups = forms.ModelMultipleChoiceField(queryset=Dojo_Group.objects.none(), required=True, label="Groups")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].disabled = True
        current_groups = Dojo_Group_Member.objects.filter(user=self.initial["user"]).values_list("group", flat=True)
        self.fields["groups"].queryset = Dojo_Group.objects.exclude(id__in=current_groups)
        self.fields["role"].queryset = get_group_member_roles()

    class Meta:
        model = Dojo_Group_Member
        fields = ["groups", "user", "role"]


class Edit_Group_MemberForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["group"].disabled = True
        self.fields["user"].disabled = True
        self.fields["role"].queryset = get_group_member_roles()

    class Meta:
        model = Dojo_Group_Member
        fields = ["group", "user", "role"]


class Delete_Group_MemberForm(Edit_Group_MemberForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].disabled = True


class Add_Product_GroupForm(forms.ModelForm):
    groups = forms.ModelMultipleChoiceField(queryset=Dojo_Group.objects.none(), required=True, label="Groups")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].disabled = True
        current_groups = Product_Group.objects.filter(product=self.initial["product"]).values_list("group", flat=True)
        authorized_groups = get_authorized_groups(Permissions.Group_View)
        authorized_groups = authorized_groups.exclude(id__in=current_groups)
        self.fields["groups"].queryset = authorized_groups

    class Meta:
        model = Product_Group
        fields = ["product", "groups", "role"]


class Add_Product_Group_GroupForm(forms.ModelForm):
    products = forms.ModelMultipleChoiceField(queryset=Product.objects.none(), required=True, label="Products")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_members = Product_Group.objects.filter(group=self.initial["group"]).values_list("product", flat=True)
        self.fields["products"].queryset = get_authorized_products(Permissions.Product_Member_Add_Owner) \
            .exclude(id__in=current_members)
        self.fields["group"].disabled = True

    class Meta:
        model = Product_Group
        fields = ["products", "group", "role"]


class Edit_Product_Group_Form(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].disabled = True
        self.fields["group"].disabled = True

    class Meta:
        model = Product_Group
        fields = ["product", "group", "role"]


class Delete_Product_GroupForm(Edit_Product_Group_Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].disabled = True


class Add_Product_Type_GroupForm(forms.ModelForm):
    groups = forms.ModelMultipleChoiceField(queryset=Dojo_Group.objects.none(), required=True, label="Groups")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_groups = Product_Type_Group.objects.filter(product_type=self.initial["product_type"]).values_list("group", flat=True)
        authorized_groups = get_authorized_groups(Permissions.Group_View)
        authorized_groups = authorized_groups.exclude(id__in=current_groups)
        self.fields["groups"].queryset = authorized_groups
        self.fields["product_type"].disabled = True

    class Meta:
        model = Product_Type_Group
        fields = ["product_type", "groups", "role"]


class Add_Product_Type_Group_GroupForm(forms.ModelForm):
    product_types = forms.ModelMultipleChoiceField(queryset=Product_Type.objects.none(), required=True, label="Product Types")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_members = Product_Type_Group.objects.filter(group=self.initial["group"]).values_list("product_type", flat=True)
        self.fields["product_types"].queryset = get_authorized_product_types(Permissions.Product_Type_Member_Add_Owner) \
            .exclude(id__in=current_members)
        self.fields["group"].disabled = True

    class Meta:
        model = Product_Type_Group
        fields = ["product_types", "group", "role"]


class Edit_Product_Type_Group_Form(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product_type"].disabled = True
        self.fields["group"].disabled = True

    class Meta:
        model = Product_Type_Group
        fields = ["product_type", "group", "role"]


class Delete_Product_Type_GroupForm(Edit_Product_Type_Group_Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["role"].disabled = True


class DojoUserForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not get_current_user().is_superuser and not get_system_setting("enable_user_profile_editable"):
            for field in self.fields:
                self.fields[field].disabled = True

    class Meta:
        model = Dojo_User
        exclude = ["password", "last_login", "is_superuser", "groups",
                   "username", "is_staff", "is_active", "date_joined",
                   "user_permissions"]


class ChangePasswordForm(forms.Form):
    current_password = forms.CharField(widget=forms.PasswordInput,
        required=True)
    new_password = forms.CharField(widget=forms.PasswordInput,
        required=True,
        validators=[validate_password],
        help_text="")
    confirm_password = forms.CharField(widget=forms.PasswordInput,
        required=True,
        validators=[validate_password],
        help_text="Password must match the new password entered above.")

    def __init__(self, *args, **kwargs):
        self.user = None
        if "user" in kwargs:
            self.user = kwargs.pop("user")
        super().__init__(*args, **kwargs)
        self.fields["new_password"].help_text = get_password_requirements_string()

    def clean(self):
        cleaned_data = super().clean()

        current_password = self.cleaned_data.get("current_password")
        new_password = self.cleaned_data.get("new_password")
        confirm_password = self.cleaned_data.get("confirm_password")

        if not self.user.check_password(current_password):
            msg = "Current password is incorrect."
            raise forms.ValidationError(msg)
        if new_password == current_password:
            msg = "New password must be different from current password."
            raise forms.ValidationError(msg)
        if new_password != confirm_password:
            msg = "Passwords do not match."
            raise forms.ValidationError(msg)

        return cleaned_data


class AddDojoUserForm(forms.ModelForm):
    email = forms.EmailField(required=True)
    password = forms.CharField(widget=forms.PasswordInput,
        required=settings.REQUIRE_PASSWORD_ON_USER,
        validators=[validate_password],
        help_text="")

    class Meta:
        model = Dojo_User
        fields = ["username", "password", "first_name", "last_name", "email", "is_active", "is_superuser"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_user = get_current_user()
        if not current_user.is_superuser:
            self.fields["is_superuser"].disabled = True
        self.fields["password"].help_text = get_password_requirements_string()


class EditDojoUserForm(forms.ModelForm):
    email = forms.EmailField(required=True)

    class Meta:
        model = Dojo_User
        fields = ["username", "first_name", "last_name", "email", "is_active", "is_superuser"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_user = get_current_user()
        if not current_user.is_superuser:
            self.fields["is_superuser"].disabled = True


class DeleteUserForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = User
        fields = ["id"]


class UserContactInfoForm(forms.ModelForm):
    class Meta:
        model = UserContactInfo
        exclude = ["user", "slack_user_id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_user = get_current_user()
        if not current_user.is_superuser:
            if not user_has_configuration_permission(current_user, "auth.change_user") and \
               not user_has_configuration_permission(current_user, "auth.add_user"):
                del self.fields["force_password_reset"]
            if not get_system_setting("enable_user_profile_editable"):
                for field in self.fields:
                    self.fields[field].disabled = True


class GlobalRoleForm(forms.ModelForm):
    class Meta:
        model = Global_Role
        exclude = ["user", "group"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current_user = get_current_user()
        if not current_user.is_superuser:
            self.fields["role"].disabled = True


def get_years():
    now = timezone.now()
    return [(now.year, now.year), (now.year - 1, now.year - 1), (now.year - 2, now.year - 2)]


class ProductCountsFormBase(forms.Form):
    month = forms.ChoiceField(choices=list(MONTHS.items()), required=True, error_messages={
        "required": "*"})
    year = forms.ChoiceField(choices=get_years, required=True, error_messages={
        "required": "*"})


class ProductTypeCountsForm(ProductCountsFormBase):
    product_type = forms.ModelChoiceField(required=True,
                                          queryset=Product_Type.objects.none(),
                                          error_messages={
                                              "required": "*"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product_type"].queryset = get_authorized_product_types(Permissions.Product_Type_View)


class ProductTagCountsForm(ProductCountsFormBase):
    product_tag = forms.ModelChoiceField(required=True,
                                         queryset=Product.tags.tag_model.objects.none().order_by("name"),
                                         error_messages={
                                             "required": "*"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        prods = get_authorized_products(Permissions.Product_View)
        tags_available_to_user = Product.tags.tag_model.objects.filter(product__in=prods)
        self.fields["product_tag"].queryset = tags_available_to_user


class APIKeyForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = User
        exclude = ["username", "first_name", "last_name", "email", "is_active",
                   "is_staff", "is_superuser", "password", "last_login", "groups",
                   "date_joined", "user_permissions"]


class ReportOptionsForm(forms.Form):
    yes_no = (("0", "No"), ("1", "Yes"))
    include_finding_notes = forms.ChoiceField(choices=yes_no, label="Finding Notes")
    include_finding_images = forms.ChoiceField(choices=yes_no, label="Finding Images")
    include_executive_summary = forms.ChoiceField(choices=yes_no, label="Executive Summary")
    include_table_of_contents = forms.ChoiceField(choices=yes_no, label="Table of Contents")
    include_disclaimer = forms.ChoiceField(choices=yes_no, label="Disclaimer")
    report_type = forms.ChoiceField(choices=(("HTML", "HTML"),))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if get_system_setting("disclaimer_reports_forced"):
            self.fields["include_disclaimer"].disabled = True
            self.fields["include_disclaimer"].initial = "1"  # represents yes
            self.fields["include_disclaimer"].help_text = "Administrator of the system enforced placement of disclaimer in all reports. You are not able exclude disclaimer from this report."


class CustomReportOptionsForm(forms.Form):
    yes_no = (("0", "No"), ("1", "Yes"))
    report_name = forms.CharField(required=False, max_length=100)
    include_finding_notes = forms.ChoiceField(required=False, choices=yes_no)
    include_finding_images = forms.ChoiceField(choices=yes_no, label="Finding Images")
    report_type = forms.ChoiceField(choices=(("HTML", "HTML"),))


class DeleteFindingForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Finding
        fields = ["id"]


class CopyFindingForm(forms.Form):
    test = forms.ModelChoiceField(
        required=True,
        queryset=Test.objects.none(),
        error_messages={"required": "*"})

    def __init__(self, *args, **kwargs):
        authorized_lists = kwargs.pop("tests", None)
        super().__init__(*args, **kwargs)
        self.fields["test"].queryset = authorized_lists


class FindingFormID(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Finding
        fields = ("id",)


class DeleteStubFindingForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Stub_Finding
        fields = ["id"]


class GITHUB_IssueForm(forms.ModelForm):

    class Meta:
        model = GITHUB_Issue
        exclude = ["product"]


class GITHUBForm(forms.ModelForm):
    api_key = forms.CharField(widget=forms.PasswordInput, required=True)

    class Meta:
        model = GITHUB_Conf
        exclude = ["product"]


class DeleteGITHUBConfForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = GITHUB_Conf
        fields = ["id"]


class ExpressGITHUBForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, required=True)
    issue_key = forms.CharField(required=True, help_text="A valid issue ID is required to gather the necessary information.")

    class Meta:
        model = GITHUB_Conf
        exclude = ["product", "epic_name_id", "open_status_key",
                    "close_status_key", "info_mapping_severity",
                    "low_mapping_severity", "medium_mapping_severity",
                    "high_mapping_severity", "critical_mapping_severity", "finding_text"]


def get_jira_issue_template_dir_choices():
    template_root = settings.JIRA_TEMPLATE_ROOT
    template_dir_list = [("", "---")]
    for base_dir, dirnames, _filenames in os.walk(template_root):
        # for filename in filenames:
        #     if base_dir.startswith(settings.TEMPLATE_DIR_PREFIX):
        #         base_dir = base_dir[len(settings.TEMPLATE_DIR_PREFIX):]
        #     template_list.append((os.path.join(base_dir, filename), filename))

        for dirname in dirnames:
            clean_base_dir = base_dir.removeprefix(settings.TEMPLATE_DIR_PREFIX)
            template_dir_list.append((str(Path(clean_base_dir) / dirname), dirname))

    logger.debug("templates: %s", template_dir_list)
    return template_dir_list


JIRA_TEMPLATE_CHOICES = sorted(get_jira_issue_template_dir_choices())


class JIRA_IssueForm(forms.ModelForm):

    class Meta:
        model = JIRA_Issue
        exclude = ["product"]


class BaseJiraForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, required=True, help_text=JIRA_Instance._meta.get_field("password").help_text, label=JIRA_Instance._meta.get_field("password").verbose_name)

    def test_jira_connection(self):
        import dojo.jira_link.helper as jira_helper
        try:
            # Attempt to validate the credentials before moving forward
            jira_helper.get_jira_connection_raw(self.cleaned_data["url"],
                                                self.cleaned_data["username"],
                                                self.cleaned_data["password"])
            logger.debug("valid JIRA config!")
        except Exception as e:
            # form only used by admins, so we can show full error message using str(e) which can help debug any problems
            message = "Unable to authenticate to JIRA. Please check the URL, username, password, captcha challenge, Network connection. Details in alert on top right. " + str(
                e)
            self.add_error("username", message)
            self.add_error("password", message)

    def clean(self):
        self.test_jira_connection()
        return self.cleaned_data


class AdvancedJIRAForm(BaseJiraForm):
    issue_template_dir = forms.ChoiceField(required=False,
                                       choices=JIRA_TEMPLATE_CHOICES,
                                       help_text="Choose the folder containing the Django templates used to render the JIRA issue description. These are stored in dojo/templates/issue-trackers. Leave empty to use the default jira_full templates.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance:
            self.fields["password"].required = False

    def clean(self):
        if self.instance and not self.cleaned_data["password"]:
            self.cleaned_data["password"] = self.instance.password
        return super().clean()

    class Meta:
        model = JIRA_Instance
        exclude = [""]


class JIRAForm(BaseJiraForm):
    issue_key = forms.CharField(required=True, help_text="A valid issue ID is required to gather the necessary information.")
    issue_template_dir = forms.ChoiceField(required=False,
                                       choices=JIRA_TEMPLATE_CHOICES,
                                       help_text="Choose the folder containing the Django templates used to render the JIRA issue description. These are stored in dojo/templates/issue-trackers. Leave empty to use the default jira_full templates.")

    class Meta:
        model = JIRA_Instance
        exclude = ["product", "epic_name_id", "open_status_key",
                    "close_status_key", "info_mapping_severity",
                    "low_mapping_severity", "medium_mapping_severity",
                    "high_mapping_severity", "critical_mapping_severity", "finding_text"]


class Benchmark_Product_SummaryForm(forms.ModelForm):

    class Meta:
        model = Benchmark_Product_Summary
        exclude = ["product", "current_level", "benchmark_type", "asvs_level_1_benchmark", "asvs_level_1_score", "asvs_level_2_benchmark", "asvs_level_2_score", "asvs_level_3_benchmark", "asvs_level_3_score"]


class DeleteBenchmarkForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Benchmark_Product_Summary
        fields = ["id"]


# class JIRA_ProjectForm(forms.ModelForm):

#     class Meta:
#         model = JIRA_Project
#         exclude = ['product']


class Product_API_Scan_ConfigurationForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    tool_configuration = forms.ModelChoiceField(
        label="Tool Configuration",
        queryset=Tool_Configuration.objects.all().order_by("name"),
        required=True,
    )

    class Meta:
        model = Product_API_Scan_Configuration
        exclude = ["product"]


class DeleteProduct_API_Scan_ConfigurationForm(forms.ModelForm):
    id = forms.IntegerField(required=True, widget=forms.widgets.HiddenInput())

    class Meta:
        model = Product_API_Scan_Configuration
        fields = ["id"]


class DeleteJIRAInstanceForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = JIRA_Instance
        fields = ["id"]


class ToolTypeForm(forms.ModelForm):
    class Meta:
        model = Tool_Type
        exclude = ["product"]

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        self.newly_created = True
        if instance is not None:
            self.newly_created = instance.pk is None
        super().__init__(*args, **kwargs)

    def clean(self):
        form_data = self.cleaned_data
        if self.newly_created:
            name = form_data.get("name")
            # Make sure this will not create a duplicate test type
            if Tool_Type.objects.filter(name=name).count() > 0:
                msg = "A Tool Type with the name already exists"
                raise forms.ValidationError(msg)

        return form_data


class RegulationForm(forms.ModelForm):
    class Meta:
        model = Regulation
        exclude = ["product"]


class AppAnalysisForm(forms.ModelForm):
    user = forms.ModelChoiceField(queryset=Dojo_User.objects.exclude(is_active=False).order_by("first_name", "last_name"), required=True)

    class Meta:
        model = App_Analysis
        exclude = ["product"]


class DeleteAppAnalysisForm(forms.ModelForm):
    class Meta:
        model = App_Analysis
        exclude = ["product", "tags"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].disabled = True
        self.fields["user"].disabled = True
        self.fields["confidence"].disabled = True
        self.fields["version"].disabled = True
        self.fields["icon"].disabled = True
        self.fields["website"].disabled = True
        self.fields["website_found"].disabled = True


class ToolConfigForm(forms.ModelForm):
    tool_type = forms.ModelChoiceField(queryset=Tool_Type.objects.all(), label="Tool Type")
    ssh = forms.CharField(widget=forms.Textarea(attrs={}), required=False, label="SSH Key")

    class Meta:
        model = Tool_Configuration
        exclude = ["product"]

    def clean(self):
        from django.core.validators import URLValidator
        form_data = self.cleaned_data

        try:
            if form_data["url"] is not None:
                url_validator = URLValidator(schemes=["ssh", "http", "https"])
                url_validator(form_data["url"])
        except forms.ValidationError:
            msg = "It does not appear as though this endpoint is a valid URL/SSH or IP address."
            raise forms.ValidationError(msg, code="invalid")

        return form_data


class SLAConfigForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # if this sla config has findings being asynchronously updated, disable the days by severity fields
        if self.instance.async_updating:
            msg = (
                "Finding SLA expiration dates are currently being recalculated. "
                "This field cannot be changed until the calculation is complete."
            )
            self.fields["critical"].disabled = True
            self.fields["enforce_critical"].disabled = True
            self.fields["critical"].widget.attrs["message"] = msg
            self.fields["high"].disabled = True
            self.fields["enforce_high"].disabled = True
            self.fields["high"].widget.attrs["message"] = msg
            self.fields["medium"].disabled = True
            self.fields["enforce_medium"].disabled = True
            self.fields["medium"].widget.attrs["message"] = msg
            self.fields["low"].disabled = True
            self.fields["enforce_low"].disabled = True
            self.fields["low"].widget.attrs["message"] = msg

    class Meta:
        model = SLA_Configuration
        fields = ["name", "description", "critical", "enforce_critical", "high", "enforce_high", "medium", "enforce_medium", "low", "enforce_low"]


class DeleteSLAConfigForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = SLA_Configuration
        fields = ["id"]


class DeleteObjectsSettingsForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Objects_Product
        fields = ["id"]


class DeleteToolProductSettingsForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Tool_Product_Settings
        fields = ["id"]


class ToolProductSettingsForm(forms.ModelForm):
    tool_configuration = forms.ModelChoiceField(queryset=Tool_Configuration.objects.all(), label="Tool Configuration")

    class Meta:
        model = Tool_Product_Settings
        fields = ["name", "description", "url", "tool_configuration", "tool_project_id"]
        exclude = ["tool_type"]
        order = ["name"]

    def clean(self):
        from django.core.validators import URLValidator
        form_data = self.cleaned_data

        try:
            if form_data["url"] is not None:
                url_validator = URLValidator(schemes=["ssh", "http", "https"])
                url_validator(form_data["url"])
        except forms.ValidationError:
            msg = "It does not appear as though this endpoint is a valid URL/SSH or IP address."
            raise forms.ValidationError(msg, code="invalid")

        return form_data


class ObjectSettingsForm(forms.ModelForm):

    # tags = forms.CharField(widget=forms.SelectMultiple(choices=[]),
    #                        required=False,
    #                        help_text="Add tags that help describe this object.  "
    #                                  "Choose from the list or add new tags.  Press TAB key to add.")

    class Meta:
        model = Objects_Product
        fields = ["path", "folder", "artifact", "name", "review_status", "tags"]
        exclude = ["product"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def clean(self):
        return self.cleaned_data


class CredMappingForm(forms.ModelForm):
    cred_user = forms.ModelChoiceField(
        queryset=Cred_Mapping.objects.all().select_related("cred_id"),
        required=False,
        label="Select a Credential",
    )

    class Meta:
        model = Cred_Mapping
        fields = ["cred_user"]
        exclude = ["product", "finding", "engagement", "test", "url", "is_authn_provider"]

    def __init__(self, *args, **kwargs):
        cred_user_queryset = kwargs.pop("cred_user_queryset", None)
        super().__init__(*args, **kwargs)
        if cred_user_queryset is not None:
            self.fields["cred_user"].queryset = cred_user_queryset


class CredMappingFormProd(forms.ModelForm):
    class Meta:
        model = Cred_Mapping
        fields = ["cred_id", "url", "is_authn_provider"]
        exclude = ["product", "finding", "engagement", "test"]


class EngagementPresetsForm(forms.ModelForm):

    notes = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=False, help_text="Description of what needs to be tested or setting up environment for testing")

    scope = forms.CharField(widget=forms.Textarea(attrs={}),
                                  required=False, help_text="Scope of Engagement testing, IP's/Resources/URL's)")

    class Meta:
        model = Engagement_Presets
        exclude = ["product"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if disclaimer := get_system_setting("disclaimer_notes"):
            self.disclaimer = disclaimer.strip()


class DeleteEngagementPresetsForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Engagement_Presets
        fields = ["id"]


class SystemSettingsForm(forms.ModelForm):
    jira_webhook_secret = forms.CharField(required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["default_group_role"].queryset = get_group_member_roles()

    def clean(self):
        cleaned_data = super().clean()
        enable_jira_value = cleaned_data.get("enable_jira")
        jira_webhook_secret_value = cleaned_data.get("jira_webhook_secret").strip()

        if enable_jira_value and not jira_webhook_secret_value:
            self.add_error("jira_webhook_secret", "This field is required when enable Jira Integration is True")

        return cleaned_data

    class Meta:
        model = System_Settings
        exclude = ["product_grade"]


class BenchmarkForm(forms.ModelForm):

    class Meta:
        model = Benchmark_Product
        exclude = ["product", "control"]


class Benchmark_RequirementForm(forms.ModelForm):

    class Meta:
        model = Benchmark_Requirement
        exclude = [""]


class NotificationsForm(forms.ModelForm):

    class Meta:
        model = Notifications
        exclude = ["template"]


class NotificationsWebhookForm(forms.ModelForm):
    class Meta:
        model = Notification_Webhooks
        exclude = []

    def __init__(self, *args, **kwargs):
        is_superuser = kwargs.pop("is_superuser", False)
        super().__init__(*args, **kwargs)
        if not is_superuser:  # Only superadmins can edit owner
            self.fields["owner"].disabled = True  # TODO: needs to be tested


class DeleteNotificationsWebhookForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].disabled = True
        self.fields["url"].disabled = True

    class Meta:
        model = Notification_Webhooks
        fields = ["id", "name", "url"]


class ProductNotificationsForm(forms.ModelForm):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.id:
            self.initial["engagement_added"] = ""
            self.initial["close_engagement"] = ""
            self.initial["test_added"] = ""
            self.initial["scan_added"] = ""
            self.initial["sla_breach"] = ""
            self.initial["sla_breach_combined"] = ""
            self.initial["risk_acceptance_expiration"] = ""

    class Meta:
        model = Notifications
        fields = ["engagement_added", "close_engagement", "test_added", "scan_added", "sla_breach", "sla_breach_combined", "risk_acceptance_expiration"]


class AjaxChoiceField(forms.ChoiceField):
    def valid_value(self, value):
        return True


class CredUserForm(forms.ModelForm):
    # selenium_script = forms.FileField(widget=forms.widgets.FileInput(
    #    attrs={"accept": ".py"}),
    #    label="Select a Selenium Script", required=False)

    class Meta:
        model = Cred_User
        exclude = [""]
        # fields = ['selenium_script']


class GITHUB_Product_Form(forms.ModelForm):
    git_conf = forms.ModelChoiceField(queryset=GITHUB_Conf.objects.all(), label="GITHUB Configuration", required=False)

    class Meta:
        model = GITHUB_PKey
        exclude = ["product"]


class JIRAProjectForm(forms.ModelForm):
    inherit_from_product = forms.BooleanField(label="inherit JIRA settings from product", required=False)
    jira_instance = forms.ModelChoiceField(queryset=JIRA_Instance.objects.all(), label="JIRA Instance", required=False)
    issue_template_dir = forms.ChoiceField(required=False,
                                       choices=JIRA_TEMPLATE_CHOICES,
                                       help_text="Choose the folder containing the Django templates used to render the JIRA issue description. These are stored in dojo/templates/issue-trackers. Leave empty to use the default jira_full templates.")

    prefix = "jira-project-form"

    class Meta:
        model = JIRA_Project
        exclude = ["product", "engagement"]
        fields = ["inherit_from_product", "jira_instance", "project_key", "issue_template_dir", "epic_issue_type_name", "component", "custom_fields", "jira_labels", "default_assignee", "enabled", "add_vulnerability_id_to_jira_label", "push_all_issues", "enable_engagement_epic_mapping", "push_notes", "product_jira_sla_notification", "risk_acceptance_expiration_notification"]

    def __init__(self, *args, **kwargs):
        from dojo.jira_link import helper as jira_helper
        # if the form is shown for an engagement, we set a placeholder text around inherited settings from product
        self.target = kwargs.pop("target", "product")
        self.product = kwargs.pop("product", None)
        self.engagement = kwargs.pop("engagement", None)
        super().__init__(*args, **kwargs)

        logger.debug("self.target: %s, self.product: %s, self.instance: %s", self.target, self.product, self.instance)
        logger.debug("data: %s", self.data)
        if self.target == "engagement":
            product_name = self.product.name if self.product else self.engagement.product.name if self.engagement.product else ""

            self.fields["project_key"].widget = forms.TextInput(attrs={"placeholder": f"JIRA settings inherited from product '{product_name}'"})
            self.fields["project_key"].help_text = f"JIRA settings are inherited from product '{product_name}', unless configured differently here."
            self.fields["jira_instance"].help_text = f"JIRA settings are inherited from product '{product_name}' , unless configured differently here."

            # if we don't have an instance, django will insert a blank empty one :-(
            # so we have to check for id to make sure we only trigger this when there is a real instance from db
            if self.instance.id:
                logger.debug("jira project instance found for engagement, unchecking inherit checkbox")
                self.fields["jira_instance"].required = True
                self.fields["project_key"].required = True
                self.initial["inherit_from_product"] = False
                # once a jira project config is attached to an engagement, we can't go back to inheriting
                # because the config needs to remain in place for the existing jira issues
                self.fields["inherit_from_product"].disabled = True
                self.fields["inherit_from_product"].help_text = "Once an engagement has a JIRA Project stored, you cannot switch back to inheritance to avoid breaking existing JIRA issues"
                self.fields["jira_instance"].disabled = False
                self.fields["project_key"].disabled = False
                self.fields["issue_template_dir"].disabled = False
                self.fields["epic_issue_type_name"].disabled = False
                self.fields["component"].disabled = False
                self.fields["custom_fields"].disabled = False
                self.fields["default_assignee"].disabled = False
                self.fields["jira_labels"].disabled = False
                self.fields["enabled"].disabled = False
                self.fields["add_vulnerability_id_to_jira_label"].disabled = False
                self.fields["push_all_issues"].disabled = False
                self.fields["enable_engagement_epic_mapping"].disabled = False
                self.fields["push_notes"].disabled = False
                self.fields["product_jira_sla_notification"].disabled = False
                self.fields["risk_acceptance_expiration_notification"].disabled = False

            elif self.product:
                logger.debug("setting jira project fields from product1")
                self.initial["inherit_from_product"] = True
                jira_project_product = jira_helper.get_jira_project(self.product)
                # we have to check that we are not in a POST request where jira project config data is posted
                # this is because initial values will overwrite the actual values entered by the user
                # makes no sense, but seems to be accepted behaviour: https://code.djangoproject.com/ticket/30407
                if jira_project_product and (self.prefix + "-jira_instance") not in self.data:
                    logger.debug("setting jira project fields from product2")
                    self.initial["jira_instance"] = jira_project_product.jira_instance.id if jira_project_product.jira_instance else None
                    self.initial["project_key"] = jira_project_product.project_key
                    self.initial["issue_template_dir"] = jira_project_product.issue_template_dir
                    self.initial["epic_issue_type_name"] = jira_project_product.epic_issue_type_name
                    self.initial["component"] = jira_project_product.component
                    self.initial["custom_fields"] = jira_project_product.custom_fields
                    self.initial["default_assignee"] = jira_project_product.default_assignee
                    self.initial["jira_labels"] = jira_project_product.jira_labels
                    self.initial["enabled"] = jira_project_product.enabled
                    self.initial["add_vulnerability_id_to_jira_label"] = jira_project_product.add_vulnerability_id_to_jira_label
                    self.initial["push_all_issues"] = jira_project_product.push_all_issues
                    self.initial["enable_engagement_epic_mapping"] = jira_project_product.enable_engagement_epic_mapping
                    self.initial["push_notes"] = jira_project_product.push_notes
                    self.initial["product_jira_sla_notification"] = jira_project_product.product_jira_sla_notification
                    self.initial["risk_acceptance_expiration_notification"] = jira_project_product.risk_acceptance_expiration_notification

                    self.fields["jira_instance"].disabled = True
                    self.fields["project_key"].disabled = True
                    self.fields["issue_template_dir"].disabled = True
                    self.fields["epic_issue_type_name"].disabled = True
                    self.fields["component"].disabled = True
                    self.fields["custom_fields"].disabled = True
                    self.fields["default_assignee"].disabled = True
                    self.fields["jira_labels"].disabled = True
                    self.fields["enabled"].disabled = True
                    self.fields["add_vulnerability_id_to_jira_label"].disabled = True
                    self.fields["push_all_issues"].disabled = True
                    self.fields["enable_engagement_epic_mapping"].disabled = True
                    self.fields["push_notes"].disabled = True
                    self.fields["product_jira_sla_notification"].disabled = True
                    self.fields["risk_acceptance_expiration_notification"].disabled = True

        else:
            del self.fields["inherit_from_product"]

        # if we don't have an instance, django will insert a blank empty one :-(
        # so we have to check for id to make sure we only trigger this when there is a real instance from db
        if self.instance.id:
            self.fields["jira_instance"].required = True
            self.fields["project_key"].required = True
            self.fields["epic_issue_type_name"].required = True

    def clean(self):
        logger.debug("validating jira project form")
        cleaned_data = super().clean()

        logger.debug("clean: inherit: %s", self.cleaned_data.get("inherit_from_product", False))
        if not self.cleaned_data.get("inherit_from_product", False):
            jira_instance = self.cleaned_data.get("jira_instance")
            project_key = self.cleaned_data.get("project_key")
            epic_issue_type_name = self.cleaned_data.get("epic_issue_type_name")

            if project_key and jira_instance and epic_issue_type_name:
                return cleaned_data

            if not project_key and not jira_instance and not epic_issue_type_name:
                return cleaned_data

            if self.target == "engagement":
                msg = "JIRA Project needs a JIRA Instance, JIRA Project Key, and Epic issue type name, or choose to inherit settings from product"
                raise ValidationError(msg)
            msg = "JIRA Project needs a JIRA Instance, JIRA Project Key, and Epic issue type name, leave empty to have no JIRA integration setup"
            raise ValidationError(msg)
        return None


class GITHUBFindingForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.enabled = kwargs.pop("enabled")
        super().__init__(*args, **kwargs)
        self.fields["push_to_github"] = forms.BooleanField()
        self.fields["push_to_github"].required = False
        self.fields["push_to_github"].help_text = "Checking this will overwrite content of your Github issue, or create one."

    push_to_github = forms.BooleanField(required=False)


class JIRAFindingForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.push_all = kwargs.pop("push_all", False)
        self.instance = kwargs.pop("instance", None)
        self.jira_project = kwargs.pop("jira_project", None)
        # we provide the finding_form from the same page so we can add validation errors
        # if the finding doesn't satisfy the rules to be pushed to JIRA
        self.finding_form = kwargs.pop("finding_form", None)

        if self.instance is None and self.jira_project is None:
            msg = "either and finding instance or jira_project is needed"
            raise ValueError(msg)

        super().__init__(*args, **kwargs)
        self.fields["push_to_jira"] = forms.BooleanField()
        self.fields["push_to_jira"].required = False
        if is_finding_groups_enabled():
            self.fields["push_to_jira"].help_text = "Checking this will overwrite content of your JIRA issue, or create one. If this finding is part of a Finding Group, the group will pushed instead of the finding."
        else:
            self.fields["push_to_jira"].help_text = "Checking this will overwrite content of your JIRA issue, or create one."

        self.fields["push_to_jira"].label = "Push to JIRA"
        if self.push_all:
            # This will show the checkbox as checked and greyed out, this way the user is aware
            # that issues will be pushed to JIRA, given their product-level settings.
            self.fields["push_to_jira"].help_text = (
                "Push all issues is enabled on this product. If you do not wish to push all issues"
                " to JIRA, please disable Push all issues on this product."
            )
            self.fields["push_to_jira"].widget.attrs["checked"] = "checked"
            self.fields["push_to_jira"].disabled = True

        if self.instance:
            if hasattr(self.instance, "has_jira_issue") and self.instance.has_jira_issue:
                self.initial["jira_issue"] = self.instance.jira_issue.jira_key
                self.fields["push_to_jira"].widget.attrs["checked"] = "checked"
        if is_finding_groups_enabled():
            self.fields["jira_issue"].widget = forms.TextInput(attrs={"placeholder": "Leave empty and check push to jira to create a new JIRA issue for this finding, or the group this finding is in."})
        else:
            self.fields["jira_issue"].widget = forms.TextInput(attrs={"placeholder": "Leave empty and check push to jira to create a new JIRA issue for this finding."})

        if self.instance and hasattr(self.instance, "has_jira_group_issue") and self.instance.has_jira_group_issue:
            self.fields["push_to_jira"].widget.attrs["checked"] = "checked"
            self.fields["jira_issue"].help_text = "Changing the linked JIRA issue for finding groups is not (yet) supported."
            self.initial["jira_issue"] = self.instance.finding_group.jira_issue.jira_key
            self.fields["jira_issue"].disabled = True

    def clean(self):
        logger.debug("jform clean")
        super().clean()
        jira_issue_key_new = self.cleaned_data.get("jira_issue")
        finding = self.instance
        jira_project = self.jira_project

        logger.debug("self.cleaned_data.push_to_jira: %s", self.cleaned_data.get("push_to_jira", None))

        if self.cleaned_data.get("push_to_jira", None) and finding and finding.has_jira_group_issue:
            can_be_pushed_to_jira, error_message, error_code = jira_helper.can_be_pushed_to_jira(finding.finding_group, self.finding_form)
            if not can_be_pushed_to_jira:
                self.add_error("push_to_jira", ValidationError(error_message, code=error_code))
                # for field in error_fields:
                #     self.finding_form.add_error(field, error)

        elif self.cleaned_data.get("push_to_jira", None) and finding:
            can_be_pushed_to_jira, error_message, error_code = jira_helper.can_be_pushed_to_jira(finding, self.finding_form)
            if not can_be_pushed_to_jira:
                self.add_error("push_to_jira", ValidationError(error_message, code=error_code))
                # for field in error_fields:
                #     self.finding_form.add_error(field, error)
        elif self.cleaned_data.get("push_to_jira", None):
            active = self.finding_form["active"].value()
            verified = self.finding_form["verified"].value()
            if not active or (not verified and (get_system_setting("enforce_verified_status", True) or get_system_setting("enforce_verified_status_jira", True))):
                logger.debug("Findings must be active and verified to be pushed to JIRA")
                error_message = "Findings must be active and verified to be pushed to JIRA"
                self.add_error("push_to_jira", ValidationError(error_message, code="not_active_or_verified"))

        if jira_issue_key_new and (not finding or not finding.has_jira_group_issue):
            # when there is a group jira issue, we skip all the linking/unlinking as this is not supported (yet)
            if finding:
                # in theory there can multiple jira instances that have similar projects
                # so checking by only the jira issue key can lead to false positives
                # so we check also the jira internal id of the jira issue
                # if the key and id are equal, it is probably the same jira instance and the same issue
                # the database model is lacking some relations to also include the jira config name or url here
                # and I don't want to change too much now. this should cover most usecases.

                jira_issue_need_to_exist = False
                # changing jira link on finding
                if finding.has_jira_issue and jira_issue_key_new != finding.jira_issue.jira_key:
                    jira_issue_need_to_exist = True

                # adding existing jira issue to finding without jira link
                if not finding.has_jira_issue:
                    jira_issue_need_to_exist = True

            else:
                jira_issue_need_to_exist = True

            if jira_issue_need_to_exist:
                jira_issue_new = jira_helper.jira_get_issue(jira_project, jira_issue_key_new)
                if not jira_issue_new:
                    raise ValidationError("JIRA issue " + jira_issue_key_new + " does not exist or cannot be retrieved")

                logger.debug("checking if provided jira issue id already is linked to another finding")
                jira_issues = JIRA_Issue.objects.filter(jira_id=jira_issue_new.id, jira_key=jira_issue_key_new).exclude(engagement__isnull=False)

                if self.instance:
                    # just be sure we exclude the finding that is being edited
                    jira_issues = jira_issues.exclude(finding=finding)

                if len(jira_issues) > 0:
                    raise ValidationError("JIRA issue " + jira_issue_key_new + " already linked to " + reverse("view_finding", args=(jira_issues[0].finding_id,)))

    jira_issue = forms.CharField(required=False, label="Linked JIRA Issue",
                validators=[validators.RegexValidator(
                    regex=r"^[A-Z][A-Z_0-9]+-\d+$",
                    message="JIRA issue key must be in XXXX-nnnn format ([A-Z][A-Z_0-9]+-\\d+)")])
    push_to_jira = forms.BooleanField(required=False, label="Push to JIRA")


class JIRAImportScanForm(forms.Form):
    def __init__(self, *args, **kwargs):
        self.push_all = kwargs.pop("push_all", False)

        super().__init__(*args, **kwargs)
        if self.push_all:
            # This will show the checkbox as checked and greyed out, this way the user is aware
            # that issues will be pushed to JIRA, given their product-level settings.
            self.fields["push_to_jira"].help_text = (
                "Push all issues is enabled on this product. If you do not wish to push all issues"
                " to JIRA, please disable Push all issues on this product."
            )
            self.fields["push_to_jira"].widget.attrs["checked"] = "checked"
            self.fields["push_to_jira"].disabled = True

    push_to_jira = forms.BooleanField(required=False, label="Push to JIRA", help_text="Checking this will create a new jira issue for each new finding.")


class JIRAEngagementForm(forms.Form):
    prefix = "jira-epic-form"

    def __init__(self, *args, **kwargs):
        self.instance = kwargs.pop("instance", None)

        super().__init__(*args, **kwargs)

        if self.instance:
            if self.instance.has_jira_issue:
                self.fields["push_to_jira"].widget.attrs["checked"] = "checked"
                self.fields["push_to_jira"].label = "Update JIRA Epic"
                self.fields["push_to_jira"].help_text = "Checking this will update the existing EPIC in JIRA."

    push_to_jira = forms.BooleanField(required=False, label="Create EPIC", help_text="Checking this will create an EPIC in JIRA for this engagement.")
    epic_name = forms.CharField(max_length=200, required=False, help_text="EPIC name in JIRA. If not specified, it defaults to the engagement name")
    epic_priority = forms.CharField(max_length=200, required=False, help_text="EPIC priority. If not specified, the JIRA default priority will be used")


class LoginBanner(forms.Form):
    banner_enable = forms.BooleanField(
        label="Enable login banner",
        initial=False,
        required=False,
        help_text="Tick this box to enable a text banner on the login page",
    )

    banner_message = forms.CharField(
        required=False,
        label="Message to display on the login page",
    )

    def clean(self):
        return super().clean()


class AnnouncementCreateForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = "__all__"


class AnnouncementRemoveForm(AnnouncementCreateForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["dismissable"].disabled = True
        self.fields["message"].disabled = True
        self.fields["style"].disabled = True


# ==============================
# Defect Dojo Engaegment Surveys
# ==============================

# List of validator_name:func_name
# Show in admin a multichoice list of validator names
# pass this to form using field_name='validator_name' ?
class QuestionForm(forms.Form):

    """Base class for a Question"""

    def __init__(self, *args, **kwargs):
        self.helper = FormHelper()
        self.helper.form_method = "post"

        # If true crispy-forms will render a <form>..</form> tags
        self.helper.form_tag = kwargs.pop("form_tag", True)

        self.engagement_survey = kwargs.get("engagement_survey")

        self.answered_survey = kwargs.get("answered_survey")
        if not self.answered_survey:
            del kwargs["engagement_survey"]
        else:
            del kwargs["answered_survey"]

        self.helper.form_class = kwargs.get("form_class", "")

        self.question = kwargs.pop("question", None)

        if not self.question:
            msg = "Need a question to render"
            raise ValueError(msg)

        super().__init__(*args, **kwargs)


class TextQuestionForm(QuestionForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # work out initial data

        initial_answer = TextAnswer.objects.filter(
            answered_survey=self.answered_survey,
            question=self.question,
        )

        initial_answer = initial_answer[0].answer if initial_answer.exists() else ""

        self.fields["answer"] = forms.CharField(
            label=self.question.text,
            widget=forms.Textarea(attrs={"rows": 3, "cols": 10}),
            required=not self.question.optional,
            initial=initial_answer,
        )

    def save(self):
        if not self.is_valid():
            msg = "form is not valid"
            raise forms.ValidationError(msg)

        answer = self.cleaned_data.get("answer")

        if not answer:
            if self.fields["answer"].required:
                msg = "Required"
                raise forms.ValidationError(msg)
            return

        text_answer, created = TextAnswer.objects.get_or_create(
            answered_survey=self.answered_survey,
            question=self.question,
        )

        if created:
            text_answer.answered_survey = self.answered_survey
        text_answer.answer = answer
        text_answer.save()


class ChoiceQuestionForm(QuestionForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        choices = [(c.id, c.label) for c in self.question.choices.all()]

        # initial values

        initial_choices = []
        choice_answer = ChoiceAnswer.objects.filter(
            answered_survey=self.answered_survey,
            question=self.question,
        ).annotate(a=Count("answer")).filter(a__gt=0)

        # we have ChoiceAnswer instance
        if choice_answer:
            choice_answer = choice_answer[0]
            initial_choices = list(choice_answer.answer.all().values_list("id", flat=True))
            if self.question.multichoice is False:
                initial_choices = initial_choices[0]

        # default classes
        widget = forms.RadioSelect
        field_type = forms.ChoiceField
        inline_type = InlineRadios

        if self.question.multichoice:
            field_type = forms.MultipleChoiceField
            widget = forms.CheckboxSelectMultiple
            inline_type = InlineCheckboxes

        field = field_type(
            label=self.question.text,
            required=not self.question.optional,
            choices=choices,
            initial=initial_choices,
            widget=widget,
        )

        self.fields["answer"] = field

        # Render choice buttons inline
        self.helper.layout = Layout(
            inline_type("answer"),
        )

    def clean_answer(self):
        real_answer = self.cleaned_data.get("answer")

        # for single choice questions, the selected answer is a single string
        if not isinstance(real_answer, list):
            real_answer = [real_answer]
        return real_answer

    def save(self):
        if not self.is_valid():
            msg = "Form is not valid"
            raise forms.ValidationError(msg)

        real_answer = self.cleaned_data.get("answer")

        if not real_answer:
            if self.fields["answer"].required:
                msg = "Required"
                raise forms.ValidationError(msg)
            return

        choices = Choice.objects.filter(id__in=real_answer)

        # find ChoiceAnswer and filter in answer !
        choice_answer = ChoiceAnswer.objects.filter(
            answered_survey=self.answered_survey,
            question=self.question,
        )

        # we have ChoiceAnswer instance
        if choice_answer:
            choice_answer = choice_answer[0]

        if not choice_answer:
            # create a ChoiceAnswer
            choice_answer = ChoiceAnswer.objects.create(
                answered_survey=self.answered_survey,
                question=self.question,
            )

        # re save out the choices
        choice_answer.answered_survey = self.answered_survey
        choice_answer.answer.set(choices)
        choice_answer.save()


class Add_Questionnaire_Form(forms.ModelForm):
    survey = forms.ModelChoiceField(
        queryset=Engagement_Survey.objects.all(),
        required=True,
        widget=forms.widgets.Select(),
        help_text="Select the Questionnaire to add.")

    class Meta:
        model = Answered_Survey
        exclude = ("responder",
                   "completed",
                   "engagement",
                   "answered_on",
                   "assignee")


class AddGeneralQuestionnaireForm(forms.ModelForm):
    survey = forms.ModelChoiceField(
        queryset=Engagement_Survey.objects.all(),
        required=True,
        widget=forms.widgets.Select(),
        help_text="Select the Questionnaire to add.")
    expiration = forms.DateField(widget=forms.TextInput(
        attrs={"class": "datepicker", "autocomplete": "off"}))

    class Meta:
        model = General_Survey
        exclude = ("num_responses", "generated")

    # date can only be today or in the past, not the future
    def clean_expiration(self):
        expiration = self.cleaned_data.get("expiration", None)
        if expiration:
            today = datetime.today().date()
            if expiration < today:
                msg = "The expiration cannot be in the past"
                raise forms.ValidationError(msg)
            if expiration.day == today.day:
                msg = "The expiration cannot be today"
                raise forms.ValidationError(msg)
        else:
            msg = "An expiration for the survey must be supplied"
            raise forms.ValidationError(msg)
        return expiration


class Delete_Questionnaire_Form(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Answered_Survey
        fields = ["id"]


class DeleteGeneralQuestionnaireForm(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = General_Survey
        fields = ["id"]


class Delete_Eng_Survey_Form(forms.ModelForm):
    id = forms.IntegerField(required=True,
                            widget=forms.widgets.HiddenInput())

    class Meta:
        model = Engagement_Survey
        fields = ["id"]


class CreateQuestionnaireForm(forms.ModelForm):
    class Meta:
        model = Engagement_Survey
        exclude = ["questions"]


with warnings.catch_warnings(action="ignore", category=ManagerInheritanceWarning):
    class EditQuestionnaireQuestionsForm(forms.ModelForm):
        questions = forms.ModelMultipleChoiceField(
            Question.polymorphic.all(),
            required=True,
            help_text="Select questions to include on this questionnaire.  Field can be used to search available questions.",
            widget=MultipleSelectWithPop(attrs={"size": "11"}))

        class Meta:
            model = Engagement_Survey
            exclude = ["name", "description", "active"]


class CreateQuestionForm(forms.Form):
    type = forms.ChoiceField(
        choices=(("---", "-----"), ("text", "Text"), ("choice", "Choice")))
    order = forms.IntegerField(
        min_value=1,
        widget=forms.TextInput(attrs={"data-type": "both"}),
        help_text="The order the question will appear on the questionnaire")
    optional = forms.BooleanField(help_text="If selected, user doesn't have to answer this question",
                                  initial=False,
                                  required=False,
                                  widget=forms.CheckboxInput(attrs={"data-type": "both"}))
    text = forms.CharField(widget=forms.Textarea(attrs={"data-type": "text"}),
                           label="Question Text",
                           help_text="The actual question.")


class CreateTextQuestionForm(forms.Form):
    class Meta:
        model = TextQuestion
        exclude = ["order", "optional"]


class MultiWidgetBasic(forms.widgets.MultiWidget):
    def __init__(self, attrs=None):
        widgets = [forms.TextInput(attrs={"data-type": "choice"}),
                   forms.TextInput(attrs={"data-type": "choice"}),
                   forms.TextInput(attrs={"data-type": "choice"}),
                   forms.TextInput(attrs={"data-type": "choice"}),
                   forms.TextInput(attrs={"data-type": "choice"}),
                   forms.TextInput(attrs={"data-type": "choice"})]
        super().__init__(widgets, attrs)

    def decompress(self, value):
        if value:
            return pickle.loads(value)
        return [None, None, None, None, None, None]

    def format_output(self, rendered_widgets):
        return "<br/>".join(rendered_widgets)


class MultiExampleField(forms.fields.MultiValueField):
    widget = MultiWidgetBasic

    def __init__(self, *args, **kwargs):
        list_fields = [forms.fields.CharField(required=True),
                       forms.fields.CharField(required=True),
                       forms.fields.CharField(required=False),
                       forms.fields.CharField(required=False),
                       forms.fields.CharField(required=False),
                       forms.fields.CharField(required=False)]
        super().__init__(list_fields, *args, **kwargs)

    def compress(self, values):
        return pickle.dumps(values)


class CreateChoiceQuestionForm(forms.Form):
    multichoice = forms.BooleanField(required=False,
                                     initial=False,
                                     widget=forms.CheckboxInput(attrs={"data-type": "choice"}),
                                     help_text="Can more than one choice can be selected?")

    answer_choices = MultiExampleField(required=False, widget=MultiWidgetBasic(attrs={"data-type": "choice"}))

    class Meta:
        model = ChoiceQuestion
        exclude = ["order", "optional", "choices"]


class EditQuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        exclude = []


class EditTextQuestionForm(EditQuestionForm):
    class Meta:
        model = TextQuestion
        exclude = []


class EditChoiceQuestionForm(EditQuestionForm):
    choices = forms.ModelMultipleChoiceField(
        Choice.objects.all(),
        required=True,
        help_text="Select choices to include on this question.  Field can be used to search available choices.",
        widget=MultipleSelectWithPop(attrs={"size": "11"}))

    class Meta:
        model = ChoiceQuestion
        exclude = []


class AddChoicesForm(forms.ModelForm):
    class Meta:
        model = Choice
        exclude = []


class AssignUserForm(forms.ModelForm):
    assignee = forms.CharField(required=False,
                                widget=forms.widgets.HiddenInput())

    def __init__(self, *args, **kwargs):
        assignee = None
        if "assignee" in kwargs:
            assignee = kwargs.pop("asignees")
        super().__init__(*args, **kwargs)
        if assignee is None:
            self.fields["assignee"] = forms.ModelChoiceField(queryset=get_authorized_users(Permissions.Engagement_View), empty_label="Not Assigned", required=False)
        else:
            self.fields["assignee"].initial = assignee

    class Meta:
        model = Answered_Survey
        exclude = ["engagement", "survey", "responder", "completed", "answered_on"]


class AddEngagementForm(forms.Form):
    product = forms.ModelChoiceField(
        queryset=Product.objects.none(),
        required=True,
        widget=forms.widgets.Select(),
        help_text="Select which product to attach Engagement")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["product"].queryset = get_authorized_products(Permissions.Engagement_Add)


class ExistingEngagementForm(forms.Form):
    engagement = forms.ModelChoiceField(
        queryset=Engagement.objects.none(),
        required=True,
        widget=forms.widgets.Select(),
        help_text="Select which Engagement to link the Questionnaire to")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["engagement"].queryset = get_authorized_engagements(Permissions.Engagement_Edit).order_by("-target_start")


class ConfigurationPermissionsForm(forms.Form):

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user", None)
        self.group = kwargs.pop("group", None)
        super().__init__(*args, **kwargs)

        self.permission_fields = get_configuration_permissions_fields()

        for permission_field in self.permission_fields:
            for codename in permission_field.codenames():
                self.fields[codename] = forms.BooleanField(required=False)
                if not get_current_user().has_perm("auth.change_permission"):
                    self.fields[codename].disabled = True

        permissions_list = Permission.objects.all()
        self.permissions = {}
        for permission in permissions_list:
            self.permissions[permission.codename] = permission

    def save(self):
        if get_current_user().is_superuser:
            for permission_field in self.permission_fields:
                for codename in permission_field.codenames():
                    self.set_permission(codename)

    def set_permission(self, codename):
        if self.cleaned_data[codename]:
            # Checkbox is set
            if self.user:
                self.user.user_permissions.add(self.permissions[codename])
            elif self.group:
                self.group.auth_group.permissions.add(self.permissions[codename])
            else:
                msg = "Neither user or group are set"
                raise Exception(msg)
        # Checkbox is unset
        elif self.user:
            self.user.user_permissions.remove(self.permissions[codename])
        elif self.group:
            self.group.auth_group.permissions.remove(self.permissions[codename])
        else:
            msg = "Neither user or group are set"
            raise Exception(msg)
