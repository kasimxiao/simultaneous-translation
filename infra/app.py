#!/usr/bin/env python3
"""
CDK App 入口 - 参数化配置

部署方式:
  cdk deploy -c vpc_id=vpc-xxx -c cognito_domain_prefix=my-translate

可用参数 (通过 -c key=value 传入):
  vpc_id              - 使用已有 VPC 的 ID（不传则自动创建新 VPC）
  cognito_domain_prefix - Cognito Hosted UI 域名前缀（全局唯一，必填）
  custom_domain       - 自定义域名，如 translate.example.com（可选）
  region              - 部署区域（默认 ap-northeast-1）

示例:
  # 使用已有 VPC
  cdk deploy -c vpc_id=vpc-xxxxxxxxx -c cognito_domain_prefix=my-translate-app

  # 自动创建 VPC + 自定义域名
  cdk deploy -c cognito_domain_prefix=my-translate -c custom_domain=translate.example.com

  # 指定账号和区域
  cdk deploy -c cognito_domain_prefix=my-translate --profile other-account
"""
import os
import aws_cdk as cdk
from stack import TranslateServiceStack

app = cdk.App()

# 从环境变量或 CDK context 获取 region
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION", "ap-northeast-1")
account = os.environ.get("CDK_DEFAULT_ACCOUNT")

TranslateServiceStack(app, "TranslateServiceStack",
    env=cdk.Environment(account=account, region=region),
)
app.synth()
