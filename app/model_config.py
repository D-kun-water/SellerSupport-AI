"""Read local configuration without changing process environment or exposing secrets."""
import os
import json
import secrets
from pathlib import Path
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urlparse
from .store import ROOT
from .domain import DomainError


def local_values(path=None):
    values = {}
    path = path or ROOT / '.env'
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('\"').strip("'")
    # Explicit shell values override local file; no unrelated variables are retained.
    for key in ('DEEPSEEK_API_KEY', 'SELLERSUPPORT_PROVIDER', 'SELLERSUPPORT_MODEL', 'SELLERSUPPORT_BASE_URL',
                'SELLERSUPPORT_AI_ENABLED', 'SELLERSUPPORT_TRANSLATION_ENABLED', 'SELLERSUPPORT_EXPERIMENT_BUDGET_CNY',
                'SELLERSUPPORT_INPUT_CNY_PER_MILLION', 'SELLERSUPPORT_OUTPUT_CNY_PER_MILLION',
                'SELLERSUPPORT_SEED_MODE', 'SELLERSUPPORT_DB', 'SELLERSUPPORT_AGENT_TOKEN', 'SELLERSUPPORT_SUPERVISOR_TOKEN'):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def credential_file(database):
    return Path(database).with_suffix('.credentials.json')


def bootstrap_tokens(database, settings):
    """Use configured identities or persist random, database-specific local credentials."""
    roles = ('agent', 'supervisor')
    configured = {role: settings.get('SELLERSUPPORT_'+role.upper()+'_TOKEN', '').strip() for role in roles}
    if all(configured.values()):
        return configured
    path = credential_file(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='utf-8') as handle:
            json.dump({role: secrets.token_urlsafe(32) for role in roles}, handle)
    except FileExistsError:
        pass
    stored = json.loads(path.read_text(encoding='utf-8'))
    return {role: configured[role] or stored[role] for role in roles}


@dataclass(frozen=True)
class ModelConfig:
    api_key: str = field(default='', repr=False)
    provider: str = 'deepseek'
    model: str = 'deepseek-flash'
    base_url: str = 'https://api.deepseek.com'
    enabled: bool = False
    translation_enabled: bool = True
    budget_cny: Decimal = Decimal('5.00')
    input_price: Decimal = Decimal('2.00')
    output_price: Decimal = Decimal('8.00')
    max_output_tokens: int = 800
    deadline_seconds: float = 25.0

    @classmethod
    def load(cls):
        v = local_values()
        try:
            config = cls(api_key=v.get('DEEPSEEK_API_KEY', ''), provider=v.get('SELLERSUPPORT_PROVIDER', 'deepseek'),
                         model=v.get('SELLERSUPPORT_MODEL', 'deepseek-flash'),
                         base_url=v.get('SELLERSUPPORT_BASE_URL', 'https://api.deepseek.com').rstrip('/'),
                         enabled=v.get('SELLERSUPPORT_AI_ENABLED', 'false').lower() == 'true',
                         translation_enabled=v.get('SELLERSUPPORT_TRANSLATION_ENABLED', 'true').lower() == 'true',
                         budget_cny=Decimal(v.get('SELLERSUPPORT_EXPERIMENT_BUDGET_CNY', '5.00')),
                         input_price=Decimal(v.get('SELLERSUPPORT_INPUT_CNY_PER_MILLION', '2.00')),
                         output_price=Decimal(v.get('SELLERSUPPORT_OUTPUT_CNY_PER_MILLION', '8.00')))
            for number in (config.budget_cny, config.input_price, config.output_price):
                if not number.is_finite() or number <= 0:
                    raise ValueError()
        except Exception:
            raise DomainError('MODEL_CONFIG_ERROR', '模型价格和预算必须为有效正数', 503)
        u = urlparse(config.base_url)
        # A configuration typo must never send credentials to an arbitrary host.
        if config.provider != 'deepseek' or u.scheme != 'https' or u.hostname != 'api.deepseek.com' or u.port or u.query or u.fragment or u.username or u.path not in ('', '/v1'):
            raise DomainError('MODEL_CONFIG_ERROR', '本适配器只允许 DeepSeek 官方 HTTPS 地址', 503)
        return config

    def public(self):
        return dict(provider=self.provider, requested_model=self.model, configured=bool(self.api_key),
                    enabled=self.enabled, translation_enabled=self.translation_enabled, thinking='disabled', max_output_tokens=self.max_output_tokens,
                    experiment_budget_cny=str(self.budget_cny), pricing_kind='list_price_estimate',
                    input_cny_per_million=str(self.input_price), output_cny_per_million=str(self.output_price))
