#!/usr/bin/env python3
"""Hello World — execute SKILL expressions and display return values locally.

Prerequisites:
- virtuoso-bridge tunnel running (virtuoso-bridge start)
- RAMIC daemon loaded in Virtuoso CIW
"""
from virtuoso_bridge import VirtuosoClient

client = VirtuosoClient.from_env()

# Print a banner in CIW
skill_cmd = r'printf("\n\n==============================================\nHello, Virtuoso!\n==============================================\n")'
r = client.execute_skill(skill_cmd)
print(f"Banner: {r.output!r}")

# Date & Time
skill_cmd = r'let((v) v=getCurrentTime() printf("[Date & Time]     %s\n" v) v)'
r = client.execute_skill(skill_cmd)
print(f"Date & Time:     {r.output}")

# Cadence Version
skill_cmd = r'let((v) v=getVersion() printf("[Cadence Version] %s\n" v) v)'
r = client.execute_skill(skill_cmd)
print(f"Cadence Version: {r.output}")

# SKILL Version
skill_cmd = r'let((v) v=getSkillVersion() printf("[SKILL Version]   %s\n" v) v)'
r = client.execute_skill(skill_cmd)
print(f"SKILL Version:   {r.output}")

# Working Directory
skill_cmd = r'let((v) v=getWorkingDir() printf("[Working Dir]     %s\n" v) v)'
r = client.execute_skill(skill_cmd)
print(f"Working Dir:     {r.output}")

# Host Name
skill_cmd = r'let((v) v=getHostName() printf("[Host Name]       %s\n" v) v)'
r = client.execute_skill(skill_cmd)
print(f"Host Name:       {r.output}")

# Simple arithmetic
skill_cmd = r'let((v) v=1+2 printf("[Arithmetic]      1+2 = %d\n" v) v)'
r = client.execute_skill(skill_cmd)
print(f"1 + 2 =          {r.output}")

# String concatenation
skill_cmd = r'let((v) v=strcat("Hello" " from SKILL") printf("[String]          %s\n" v) v)'
r = client.execute_skill(skill_cmd)
print(f"strcat:          {r.output}")
