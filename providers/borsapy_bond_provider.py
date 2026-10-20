"""
Borsapy Bond Provider
Provides Turkish government bond yields scraped from doviz.com/tahvil.
Supports 2Y, 5Y, and 10Y maturities with real-time data.
"""
import asyncio
import logging
import re
from typing import Dict, Any, Optional, List

from bs4 import BeautifulSoup
from borsapy._providers.dovizcom_tahvil import DovizcomTahvilProvider
from borsapy.cache import TTL
from borsapy.exceptions import APIError, DataNotAvailableError

logger = logging.getLogger(__name__)


class DovizcomBondsPageProvider(DovizcomTahvilProvider):
    """borsapy's doviz.com bond scraper, updated for the current page markup.

    borsapy 0.11.0 (the latest release) looks for ``<table id="commodities">``,
    but doviz.com now serves ``<table id="bonds">``, which also lists US and UK
    bonds next to the Turkish ones. This keeps borsapy's HTTP client, cache and
    output shape and only replaces the parsing.
    """

    # The legacy id is still accepted in case doviz.com rolls the change back.
    TABLE_IDS = ("bonds", "commodities")
    CACHE_KEY = "borsa_mcp:dovizcom:tahvil:tr"
    _MATURITY_RE = re.compile(r"(\d+)\s*[- ]?\s*y[ıi]ll[ıi]k", re.IGNORECASE)

    def _parse_float(self, text: str) -> float | None:
        cleaned = (text or "").strip().replace("%", "").replace(" ", "")
        if "," in cleaned:
            # Turkish format: "1.234,56" -> "1234.56"
            cleaned = cleaned.replace(".", "").replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None

    def _get_maturity(self, name: str) -> str | None:
        match = self._MATURITY_RE.search(name)
        return f"{match.group(1)}Y" if match else None

    @staticmethod
    def _is_turkish(name: str, url: str) -> bool:
        return name.upper().startswith("TR ") or "/tahvil/tr-" in url

    def parse_bonds_html(self, html: str | bytes) -> list[dict[str, Any]]:
        """Parse the Turkish bonds out of a doviz.com/tahvil page."""
        soup = BeautifulSoup(html, "html.parser")
        table = next(
            (t for t in (soup.find("table", id=i) for i in self.TABLE_IDS) if t),
            None,
        )
        if table is None:
            raise DataNotAvailableError(
                "doviz.com/tahvil markup changed: no bond table "
                f"(looked for id={' or '.join(self.TABLE_IDS)})"
            )

        bonds = []
        for row in (table.find("tbody") or table).find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            link = cells[0].find("a")
            url = link.get("href", "") if link else ""
            name = (row.get("data-name") or cells[0].get_text(" ", strip=True)).strip()
            if not self._is_turkish(name, url):
                continue
            maturity = self._get_maturity(name)
            yield_rate = self._parse_float(cells[1].get_text())
            if maturity is None or yield_rate is None:
                continue
            change_pct = self._parse_float(cells[2].get_text())
            bonds.append({
                "name": name,
                "maturity": maturity,
                "yield": yield_rate,
                "yield_decimal": yield_rate / 100,
                "change": yield_rate * change_pct / 100 if change_pct is not None else None,
                "change_pct": change_pct,
                "url": url,
            })

        if not bonds:
            raise DataNotAvailableError(
                "doviz.com/tahvil bond table has no parsable Turkish bond rows"
            )
        return bonds

    def get_bond_yields(self) -> list[dict[str, Any]]:
        cached = self._cache_get(self.CACHE_KEY)
        if cached is not None:
            return cached
        try:
            response = self._get(self.BASE_URL)
        except Exception as e:
            raise APIError(f"Failed to fetch doviz.com/tahvil: {e}") from e
        bonds = self.parse_bonds_html(response.content)
        self._cache_set(self.CACHE_KEY, bonds, TTL.FX_RATES)
        return bonds


_bonds_page_provider: Optional[DovizcomBondsPageProvider] = None


def get_bonds_page_provider() -> DovizcomBondsPageProvider:
    """Shared instance, so repeated tool calls hit the 5-minute cache."""
    global _bonds_page_provider
    if _bonds_page_provider is None:
        _bonds_page_provider = DovizcomBondsPageProvider()
    return _bonds_page_provider


class BorsapyBondProvider:
    """Turkish government bond yields from doviz.com."""

    # Supported maturities
    SUPPORTED_MATURITIES = ["2Y", "5Y", "10Y"]

    # Bond name mapping
    BOND_NAMES = {
        "2Y": "T.C. Hazine Müsteşarlığı 2 Yıllık Tahvil",
        "5Y": "T.C. Hazine Müsteşarlığı 5 Yıllık Tahvil",
        "10Y": "T.C. Hazine Müsteşarlığı 10 Yıllık Tahvil"
    }

    def __init__(self, page_provider: Optional[DovizcomBondsPageProvider] = None):
        """Initialize bond provider."""
        self._page_provider = page_provider
        logger.info("Initialized Borsapy Bond Provider")

    @property
    def page_provider(self) -> DovizcomBondsPageProvider:
        return self._page_provider or get_bonds_page_provider()

    def _fetch_bonds_by_maturity(self) -> Dict[str, Dict[str, Any]]:
        """Synchronous fetch; raises if doviz.com yields nothing usable."""
        return {b["maturity"]: b for b in self.page_provider.get_bond_yields()}

    async def get_tahvil_faizleri(self) -> Dict[str, Any]:
        """
        Fetch current Turkish government bond yields for all maturities.

        Returns:
            Dict containing bond yields for 2Y, 5Y, and 10Y maturities:
            {
                'tahviller': [...],  # List of bond data
                'toplam_tahvil': 3,  # Count
                'tahvil_lookup': {'2Y': 0.28, '5Y': 0.29, '10Y': 0.31},  # Quick lookup
                'kaynak_url': 'borsapy (doviz.com)',
                'not': 'Faiz oranları yüzde (%) olarak verilmiştir...'
            }
            On failure the lists are empty and 'error' says why.
        """
        try:
            logger.info("Fetching bond yields from doviz.com")
            by_maturity = await asyncio.to_thread(self._fetch_bonds_by_maturity)
        except Exception as e:
            logger.error(f"Error fetching bond yields: {e}")
            return {
                'error': str(e),
                'tahviller': [],
                'toplam_tahvil': 0,
                'tahvil_lookup': {},
                'kaynak_url': 'borsapy (doviz.com)'
            }

        tahviller: List[Dict[str, Any]] = []
        tahvil_lookup: Dict[str, float] = {}
        errors = []

        for maturity in self.SUPPORTED_MATURITIES:
            bond = by_maturity.get(maturity)
            if bond is None:
                logger.error(f"{maturity} bond missing from doviz.com/tahvil")
                errors.append(f"{maturity}: not listed on doviz.com/tahvil")
                continue

            tahviller.append({
                'tahvil_adi': self.BOND_NAMES.get(maturity, f'TR {maturity} Bond'),
                'vade': maturity,
                'faiz_orani': bond['yield'],
                'faiz_orani_decimal': bond['yield_decimal'],
                'degisim_yuzde': bond['change_pct'],
                'tahvil_url': bond.get('url') or 'https://www.doviz.com/tahvil'
            })
            tahvil_lookup[maturity] = bond['yield_decimal']
            logger.info(f"Fetched {maturity} bond: {bond['yield']}%")

        result = {
            'tahviller': tahviller,
            'toplam_tahvil': len(tahviller),
            'tahvil_lookup': tahvil_lookup,
            'kaynak_url': 'borsapy (doviz.com)',
            'not': 'Faiz oranları yüzde (%) olarak verilmiştir. Decimal değerler için faiz_orani_decimal kullanın.'
        }

        if errors:
            result['uyarilar'] = errors

        return result

    async def get_10y_tahvil_faizi(self) -> Optional[float]:
        """
        Get current 10-year Turkish government bond yield as decimal.

        This is a convenience method for use in DCF calculations.

        Returns:
            10Y bond yield as decimal (e.g., 0.3179 for 31.79%), or None on error
        """
        try:
            return await asyncio.to_thread(self.get_risk_free_rate)
        except Exception as e:
            logger.error(f"Error getting 10Y bond yield: {e}")
            return None

    def get_risk_free_rate(self) -> Optional[float]:
        """
        Synchronous convenience method for getting risk-free rate.

        Replaces bp.risk_free_rate(), which reads the same doviz.com page with
        borsapy's outdated parser and returns None.

        Returns:
            10Y bond yield as decimal, or None on error
        """
        try:
            bond = self._fetch_bonds_by_maturity().get("10Y")
            return bond["yield_decimal"] if bond else None
        except Exception as e:
            logger.error(f"Error getting risk-free rate: {e}")
            return None
