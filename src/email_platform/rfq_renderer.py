"""The one and only renderer for RFQ email bodies.

Every provider sends the exact same markup, produced here from a Jinja
template under ``templates/emails/`` — there is no per-provider body and no
``text/plain`` alternative anywhere in the pipeline (requirement 8). Two
templates exist, chosen by supplier type:

============== ==========================================================
supplier_type  Template
============== ==========================================================
``chinese``    ``emails/rfq_email_zh.html`` (Simplified Chinese copy)
anything else  ``emails/rfq_email.html`` (English copy)
============== ==========================================================

:meth:`~src.email_platform.email_master.EmailMaster.build_rfq_html`
delegates here, so provider code and the service layer keep one call site.

Example:
    >>> from src.config import get_settings
    >>> from src.email_platform.rfq_renderer import RfqRenderer
    >>> html = RfqRenderer(get_settings()).render(   # doctest: +SKIP
    ...     supplier_type="chinese", company="IMS Flow",
    ...     conv_id="hd273hsd", supplier_name="Acme",
    ...     product_name="X200", quantity=500, target_price="$12.00")
    >>> "询价" in html                                # doctest: +SKIP
    True
"""

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config import Settings

# Template chosen when the supplier is Chinese; everything else uses the
# English one. Kept as constants so the Alibaba/SendCloud region routing and
# the renderer agree on exactly one spelling of the supplier type.
_TEMPLATE_ZH = "emails/rfq_email_zh.html"
_TEMPLATE_EN = "emails/rfq_email.html"
_SUPPLIER_TYPE_CHINESE = "chinese"


class RfqRenderer:
    """Render the shared RFQ body. HTML only — never a text/plain part.

    Attributes:
        settings (Settings): Shared application configuration (only
            ``templates_dir`` is read).

    Example:
        >>> renderer = RfqRenderer(settings)          # doctest: +SKIP
        >>> renderer.render(supplier_type="non_chinese", company="Acme",
        ...                 conv_id="hd273hsd", supplier_name="Widgets Ltd",
        ...                 product_name="X200", quantity=500,
        ...                 target_price="$12.00")     # doctest: +SKIP
        '<div style="font-family: Arial...'
    """

    def __init__(self, settings: Settings) -> None:
        """Build a Jinja environment rooted at the app's templates directory.

        Autoescaping is on for HTML, which is what keeps a supplier name
        containing ``<`` or ``&`` from breaking (or injecting into) the
        rendered body.

        Args:
            settings (Settings): Shared application configuration.

        Returns:
            None
        """
        self.settings = settings
        self._env = Environment(
            loader=FileSystemLoader(str(settings.templates_dir)),
            autoescape=select_autoescape(["html"]),
        )

    def render(
        self,
        *,
        supplier_type: str,
        company: str,
        conv_id: str,
        supplier_name: str,
        product_name: str,
        quantity,
        target_price: str,
    ) -> str:
        """Render the RFQ body for one conversation.

        Args:
            supplier_type (str): ``"chinese"`` selects the Chinese template;
                anything else (including ``""``/``None``) uses English.
            company (str): Sending company display name, used in the banner
                and the signature.
            conv_id (str): The conversation id shown in the reference footer.
            supplier_name (str): Salutation name for the supplier.
            product_name (str): Product being quoted.
            quantity: Number of units requested.
            target_price (str): Buyer's target unit price, e.g. ``"$12.00"``.

        Returns:
            str: The rendered HTML body.
        """
        name = (
            _TEMPLATE_ZH
            if (supplier_type or "").strip().lower() == _SUPPLIER_TYPE_CHINESE
            else _TEMPLATE_EN
        )
        return self._env.get_template(name).render(
            company=company,
            conv_id=conv_id,
            supplier_name=supplier_name,
            product_name=product_name,
            quantity=quantity,
            target_price=target_price,
        )
