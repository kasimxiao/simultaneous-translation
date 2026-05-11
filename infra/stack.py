from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_elasticloadbalancingv2 as elbv2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_iam as iam,
    aws_logs as logs,
    aws_ecr_assets as ecr_assets,
    aws_cognito as cognito,
    CfnOutput,
)
from constructs import Construct
import os


class TranslateServiceStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        # ==================== 参数读取 ====================
        vpc_id = self.node.try_get_context("vpc_id")
        cognito_domain_prefix = self.node.try_get_context("cognito_domain_prefix")
        custom_domain = self.node.try_get_context("custom_domain")  # 可选

        if not cognito_domain_prefix:
            raise ValueError(
                "必须提供 cognito_domain_prefix 参数（全局唯一）。"
                "用法: cdk deploy -c cognito_domain_prefix=my-translate-app"
            )

        deploy_region = self.region  # 从 env 获取

        # ==================== VPC ====================
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "TranslateVpc", vpc_id=vpc_id)
        else:
            # 自动创建 VPC（2 AZ，带 NAT Gateway）
            vpc = ec2.Vpc(self, "TranslateVpc",
                max_azs=2,
                nat_gateways=1,
            )

        # ==================== ECS Cluster ====================
        cluster = ecs.Cluster(self, "TranslateCluster", vpc=vpc)

        # ==================== Task Definition ====================
        task_def = ecs.FargateTaskDefinition(self, "TranslateTask",
            memory_limit_mib=512,
            cpu=256,
        )

        # IAM permissions
        task_def.task_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonTranscribeFullAccess")
        )
        task_def.task_role.add_to_policy(iam.PolicyStatement(
            actions=[
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
                "polly:SynthesizeSpeech",
            ],
            resources=["*"],
        ))

        # Container
        container = task_def.add_container("app",
            image=ecs.ContainerImage.from_asset(
                os.path.join(os.path.dirname(__file__), ".."),
                file="Dockerfile",
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="translate",
                log_retention=logs.RetentionDays.ONE_WEEK,
            ),
            environment={
                "AWS_DEFAULT_REGION": deploy_region,
            },
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8080))

        # ==================== ALB + Fargate Service ====================
        fargate_service = ecs_patterns.ApplicationLoadBalancedFargateService(
            self, "TranslateService",
            cluster=cluster,
            task_definition=task_def,
            desired_count=1,
            public_load_balancer=True,
            listener_port=80,
        )

        # Health check
        fargate_service.target_group.configure_health_check(
            path="/",
            healthy_http_codes="200",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(10),
        )

        # Stickiness for WebSocket
        fargate_service.target_group.set_attribute(
            key="stickiness.enabled", value="true",
        )
        fargate_service.target_group.set_attribute(
            key="stickiness.type", value="lb_cookie",
        )
        fargate_service.target_group.set_attribute(
            key="stickiness.lb_cookie.duration_seconds", value="86400",
        )

        # ==================== CloudFront ====================
        alb_origin = origins.LoadBalancerV2Origin(
            fargate_service.load_balancer,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
        )

        distribution = cloudfront.Distribution(self, "TranslateCDN",
            default_behavior=cloudfront.BehaviorOptions(
                origin=alb_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            ),
            additional_behaviors={
                "/socket.io/*": cloudfront.BehaviorOptions(
                    origin=alb_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                    cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                    origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                ),
            },
        )

        # ==================== Cognito ====================
        user_pool = cognito.UserPool(self, "TranslateUserPool",
            user_pool_name="translate-user-pool",
            self_sign_up_enabled=True,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )

        user_pool_domain = user_pool.add_domain("TranslateCognitoDomain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=cognito_domain_prefix,
            ),
        )

        # 构建 callback URLs
        cloudfront_url = f"https://{distribution.distribution_domain_name}"
        callback_urls = [
            cloudfront_url + "/callback",
            "http://localhost:8080/callback",
        ]
        logout_urls = [
            cloudfront_url + "/",
            "http://localhost:8080/",
        ]

        # 如果有自定义域名，加入 callback 列表
        if custom_domain:
            callback_urls.insert(0, f"https://{custom_domain}/callback")
            logout_urls.insert(0, f"https://{custom_domain}/")

        user_pool_client = user_pool.add_client("TranslateAppClient",
            user_pool_client_name="translate-web-client",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
            ),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(
                    authorization_code_grant=True,
                    implicit_code_grant=True,
                ),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO,
            ],
        )

        # ==================== 环境变量注入容器 ====================
        container.add_environment("COGNITO_USER_POOL_ID", user_pool.user_pool_id)
        container.add_environment("COGNITO_CLIENT_ID", user_pool_client.user_pool_client_id)
        container.add_environment("COGNITO_DOMAIN",
            f"{user_pool_domain.domain_name}.auth.{deploy_region}.amazoncognito.com")
        container.add_environment("COGNITO_REGION", deploy_region)

        # ==================== Outputs ====================
        CfnOutput(self, "ALBUrl",
            value=f"http://{fargate_service.load_balancer.load_balancer_dns_name}",
            description="ALB URL (direct)",
        )
        CfnOutput(self, "CloudFrontUrl",
            value=cloudfront_url,
            description="CloudFront URL (use this)",
        )
        CfnOutput(self, "CognitoUserPoolId",
            value=user_pool.user_pool_id,
            description="Cognito User Pool ID",
        )
        CfnOutput(self, "CognitoClientId",
            value=user_pool_client.user_pool_client_id,
            description="Cognito App Client ID",
        )
        CfnOutput(self, "CognitoDomain",
            value=f"https://{user_pool_domain.domain_name}.auth.{deploy_region}.amazoncognito.com",
            description="Cognito Hosted UI Domain",
        )
