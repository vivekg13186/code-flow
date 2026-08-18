"""Sample workflow 1: order processing — conditions, retries, branching."""
import random
from dataclasses import dataclass, field
from typing import Literal

from engine import Workflow, flow, step


@dataclass
class MyDataInput:
    """My Data Input"""
    name: str = "Username"


class MyWorkflow(Workflow):
    """just for trying out the workflow engine"""
    tags = ["demo"]
    inputs = MyDataInput

    @flow
    def main(self, ctx):
        for i in range(3):
            self.print_name(ctx["name"], i)
            if i == 2:
                self.on_index_2(ctx["name"])

    @step()
    def print_name(self, name,index):
        return f"Hello {name}! This is iteration {index+1}"

    @step()
    def on_index_2(self, name):
        return f"Hello {name}! This is the second step"    
    
