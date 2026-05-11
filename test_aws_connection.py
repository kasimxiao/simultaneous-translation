#!/usr/bin/env python3
"""
AWS连接诊断脚本
用于测试AWS凭证和Transcribe服务连接
"""

import boto3
import sys

def test_aws_credentials():
    """测试AWS凭证"""
    print("=" * 50)
    print("1. 测试AWS凭证")
    print("=" * 50)
    
    try:
        sts = boto3.client('sts', region_name='us-west-2')
        identity = sts.get_caller_identity()
        print(f"✓ AWS凭证验证成功")
        print(f"  账户ID: {identity['Account']}")
        print(f"  用户ARN: {identity['Arn']}")
        return True
    except Exception as e:
        print(f"✗ AWS凭证验证失败: {e}")
        print("\n请检查:")
        print("  1. ~/.aws/credentials 文件是否存在")
        print("  2. AWS_ACCESS_KEY_ID 和 AWS_SECRET_ACCESS_KEY 环境变量")
        print("  3. IAM角色配置（如果在EC2上运行）")
        return False

def test_transcribe_permissions():
    """测试Transcribe权限"""
    print("\n" + "=" * 50)
    print("2. 测试Transcribe服务权限")
    print("=" * 50)
    
    try:
        transcribe = boto3.client('transcribe', region_name='us-west-2')
        # 尝试列出转录任务（不需要实际有任务）
        response = transcribe.list_transcription_jobs(MaxResults=1)
        print(f"✓ Transcribe服务访问成功")
        return True
    except Exception as e:
        print(f"✗ Transcribe服务访问失败: {e}")
        print("\n请检查IAM权限，需要以下权限:")
        print("  - transcribe:StartStreamTranscription")
        print("  - transcribe:ListTranscriptionJobs")
        return False

def test_bedrock_permissions():
    """测试Bedrock权限"""
    print("\n" + "=" * 50)
    print("3. 测试Bedrock服务权限")
    print("=" * 50)
    
    try:
        bedrock = boto3.client('bedrock-runtime', region_name='us-west-2')
        print(f"✓ Bedrock客户端创建成功")
        print(f"  注意: 实际调用需要在运行时测试")
        return True
    except Exception as e:
        print(f"✗ Bedrock客户端创建失败: {e}")
        return False

def test_network():
    """测试网络连接"""
    print("\n" + "=" * 50)
    print("4. 测试网络连接")
    print("=" * 50)
    
    import socket
    
    endpoints = [
        ("transcribestreaming.us-west-2.amazonaws.com", 8443),
        ("bedrock-runtime.us-west-2.amazonaws.com", 443),
    ]
    
    all_ok = True
    for host, port in endpoints:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            result = sock.connect_ex((host, port))
            sock.close()
            
            if result == 0:
                print(f"✓ {host}:{port} 可达")
            else:
                print(f"✗ {host}:{port} 不可达")
                all_ok = False
        except Exception as e:
            print(f"✗ {host}:{port} 连接错误: {e}")
            all_ok = False
    
    return all_ok

def main():
    print("\nAWS Transcribe 连接诊断工具\n")
    
    results = []
    results.append(("AWS凭证", test_aws_credentials()))
    results.append(("Transcribe权限", test_transcribe_permissions()))
    results.append(("Bedrock权限", test_bedrock_permissions()))
    results.append(("网络连接", test_network()))
    
    print("\n" + "=" * 50)
    print("诊断总结")
    print("=" * 50)
    
    for name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        print(f"{name}: {status}")
    
    all_passed = all(r[1] for r in results)
    
    if all_passed:
        print("\n✓ 所有测试通过！可以启动应用。")
        return 0
    else:
        print("\n✗ 部分测试失败，请根据上述信息修复问题。")
        return 1

if __name__ == '__main__':
    sys.exit(main())
