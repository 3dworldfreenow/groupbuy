"""WTForms definitions (CSRF-protected via Flask-WTF)."""
from flask_wtf import FlaskForm
from wtforms import (BooleanField, DecimalField, EmailField, IntegerField, PasswordField, SelectField,
                     StringField, TextAreaField)
from wtforms.validators import DataRequired, Email, EqualTo, Length, NumberRange, Optional, Regexp

PHONE = Regexp(r"^[0-9+()\-\s]{7,20}$", message="Enter a valid phone number.")


class LoginForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=128)])
    remember = BooleanField("Keep me signed in")


class RegisterForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    phone = StringField("Mobile number", validators=[DataRequired(), PHONE])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=10, max=128)])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password",
                                                                                    message="Passwords must match.")])
    accept_terms = BooleanField("I accept the Terms of Service and Privacy Policy", validators=[DataRequired()])


class VendorRegisterForm(RegisterForm):
    business_name = StringField("Business name", validators=[DataRequired(), Length(max=160)])
    business_address = StringField("Business address", validators=[DataRequired(), Length(max=300)])
    capacity_per_order = DecimalField("Capacity per group order (total units)", places=3,
                                      validators=[DataRequired(), NumberRange(min=1, max=1000000)])
    description = TextAreaField("About your business", validators=[Optional(), Length(max=2000)])


class ProfileForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    phone = StringField("Mobile number", validators=[DataRequired(), PHONE])


class PasswordChangeForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField("New password", validators=[DataRequired(), Length(min=10, max=128)])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("new_password")])


class AddressForm(FlaskForm):
    label = StringField("Label", validators=[DataRequired(), Length(max=40)], default="Home")
    recipient_name = StringField("Recipient name", validators=[DataRequired(), Length(max=120)])
    phone = StringField("Phone", validators=[DataRequired(), PHONE])
    line1 = StringField("Address line 1", validators=[DataRequired(), Length(max=200)])
    line2 = StringField("Address line 2", validators=[Optional(), Length(max=200)])
    city = StringField("City", validators=[DataRequired(), Length(max=80)])
    state = StringField("State", validators=[Optional(), Length(max=80)])
    postal_code = StringField("Postal code", validators=[DataRequired(), Regexp(r"^[A-Za-z0-9\- ]{3,12}$")])
    location_id = SelectField("Neighbourhood", coerce=int, validators=[Optional()])
    is_default = BooleanField("Use as my default address")


class GroupForm(FlaskForm):
    name = StringField("Group name", validators=[DataRequired(), Length(min=3, max=120)])
    description = TextAreaField("Description", validators=[Optional(), Length(max=1000)])
    location_id = SelectField("Location", coerce=int, validators=[DataRequired()])
    pickup_point_id = SelectField("Community pickup point", coerce=int, validators=[Optional()])
    is_public = BooleanField("List this group publicly so neighbours can find and join it (private groups are joined by invitation code or link only)",
                             default=True)


class VendorProfileForm(FlaskForm):
    business_name = StringField("Business name", validators=[DataRequired(), Length(max=160)])
    legal_name = StringField("Legal entity name", validators=[Optional(), Length(max=160)])
    registration_number = StringField("Business registration number", validators=[Optional(), Length(max=80)])
    tax_id = StringField("Tax registration (e.g. GSTIN)", validators=[Optional(), Length(max=40)])
    food_licence_number = StringField("Food safety licence number", validators=[Optional(), Length(max=60)])
    contact_phone = StringField("Contact phone", validators=[DataRequired(), PHONE])
    business_address = StringField("Business address", validators=[DataRequired(), Length(max=300)])
    capacity_per_order = DecimalField("Capacity per group order (total units)", places=3,
                                      validators=[DataRequired(), NumberRange(min=1, max=1000000)])
    description = TextAreaField("About your business", validators=[Optional(), Length(max=2000)])


class CategoryForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80)])
    slug = StringField("Slug", validators=[DataRequired(), Regexp(r"^[a-z0-9-]{2,80}$")])
    description = StringField("Description", validators=[Optional(), Length(max=300)])
    sort_order = IntegerField("Sort order", default=0, validators=[Optional()])
    is_active = BooleanField("Active", default=True)


class ProductForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=80)])
    slug = StringField("Slug", validators=[DataRequired(), Regexp(r"^[a-z0-9-]{2,80}$")])
    category_id = SelectField("Category", coerce=int, validators=[DataRequired()])
    unit = SelectField("Unit", validators=[DataRequired()])
    emoji = StringField("Icon (emoji)", validators=[Optional(), Length(max=8)])
    description = StringField("Description", validators=[Optional(), Length(max=500)])
    min_customer_qty = DecimalField("Minimum per customer", places=3, validators=[DataRequired(),
                                                                                  NumberRange(min=0.001)])
    qty_step = DecimalField("Quantity step", places=3, validators=[DataRequired(), NumberRange(min=0.001)])
    max_customer_qty = DecimalField("Maximum per customer", places=3, validators=[Optional(),
                                                                                  NumberRange(min=0.001)])
    is_active = BooleanField("Active", default=True)


class GradeForm(FlaskForm):
    code = StringField("Code", validators=[DataRequired(), Regexp(r"^[A-Z0-9_]{2,16}$")])
    name = StringField("Name", validators=[DataRequired(), Length(max=60)])
    rank = IntegerField("Rank (higher is better)", validators=[DataRequired(), NumberRange(min=1, max=100)])
    specification = TextAreaField("Specification", validators=[DataRequired(), Length(max=2000)])
    max_defect_percent = DecimalField("Maximum defects (%)", places=2,
                                      validators=[DataRequired(), NumberRange(min=0, max=100)])
    is_active = BooleanField("Active", default=True)


class LocationForm(FlaskForm):
    name = StringField("Neighbourhood / area", validators=[DataRequired(), Length(max=120)])
    city = StringField("City", validators=[DataRequired(), Length(max=80)])
    state = StringField("State", validators=[Optional(), Length(max=80)])
    country = StringField("Country code", default="IN", validators=[DataRequired(), Regexp(r"^[A-Z]{2}$")])
    is_active = BooleanField("Active", default=True)


class PickupPointForm(FlaskForm):
    location_id = SelectField("Location", coerce=int, validators=[DataRequired()])
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    address_text = StringField("Address", validators=[DataRequired(), Length(max=300)])
    instructions = StringField("Instructions", validators=[Optional(), Length(max=300)])
    is_active = BooleanField("Active", default=True)


class ContactForm(FlaskForm):
    name = StringField("Your name", validators=[DataRequired(), Length(max=120)])
    email = EmailField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    subject = StringField("Subject", validators=[DataRequired(), Length(max=160)])
    message = TextAreaField("Message", validators=[DataRequired(), Length(min=10, max=5000)])
