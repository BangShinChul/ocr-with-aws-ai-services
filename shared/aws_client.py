import logging
from functools import lru_cache

import boto3

from shared.config import AWS_PROFILE, AWS_REGION

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_aws_session(region=None):
    """One boto3 session for the whole app, built from AWS_PROFILE / AWS_REGION."""
    return boto3.session.Session(profile_name=AWS_PROFILE, region_name=region or AWS_REGION)


@lru_cache(maxsize=16)
def get_aws_client(service_name, region=None, endpoint_url=None):
    """Cached client from the shared session (same profile and Region everywhere)."""
    kwargs = {}
    if region:
        kwargs["region_name"] = region
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return get_aws_session().client(service_name, **kwargs)


def get_account_id():
    return get_aws_client("sts").get_caller_identity()["Account"]


def get_current_region():
    return get_aws_session().region_name


# Bedrock Data Automation cross-Region profile per source Region.
# https://docs.aws.amazon.com/bedrock/latest/userguide/bda-cris.html
def bda_profile_prefix(region: str) -> str:
    if region == "ap-southeast-1":
        return "global"
    if region.startswith("us-gov-"):
        return "us-gov"
    if region.startswith("us-"):
        return "us"
    if region.startswith("ca-"):
        return "na"
    if region.startswith("eu-"):
        return "eu"
    if region.startswith("ap-"):
        return "apac"
    return "global"


def bda_profile_arn(region: str, account_id: str) -> str:
    return f"arn:aws:bedrock:{region}:{account_id}:data-automation-profile/{bda_profile_prefix(region)}.data-automation-v1"
