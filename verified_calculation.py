"""Bounded arithmetic on numbers explicitly supplied in a calculation question.

No model extraction, arbitrary eval, source lookup, tools or external calls.
Returns None for questions outside the supported grammar, preserving routing.
"""
import ast
from decimal import Decimal, DecimalException, Inexact, localcontext
import re

NUMBER = r'(?:\d+(?:\.\d*)?|\.\d+)'


def answer(question):
    if not isinstance(question, str):
        return None
    text = question.strip().removesuffix('?').strip()
    text = re.sub(r"^(?:what is|what's|calculate|compute|evaluate)\s+", '', text, flags=re.I)
    text = text.replace('×', '*').replace('÷', '/').replace('−', '-')
    for word, symbol in [('plus', '+'), ('minus', '-'), ('times', '*'), ('divided by', '/')]:
        text = re.sub(r'\b' + word + r'\b', symbol, text, flags=re.I)
    summed = re.fullmatch(r'(?:the\s+)?sum(?:\s+of)?\s*:?\s+(.+)', text, re.I)
    body = summed[1].strip() if summed else text
    # Full-match only: never discard an additional ask, units or source text.
    if not body or not re.fullmatch(r'[\d\s.,+*/()\-]+', body, re.ASCII):
        return None
    if not summed and not any(c in body for c in '+-*/'):
        return None
    if not any(c.isdigit() for c in body):
        return None
    if len(body) > 2000:
        return 'Calculation refused: the expression is too long (maximum 2,000 characters).'
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            ctx.clear_flags()
            def numeric(token):
                if not re.fullmatch(NUMBER, token) or len(token.replace('.', '')) > 40:
                    raise ValueError('number_limit')
                return Decimal(token)
            if summed:
                if re.search(r'(?<![\d.])\d{1,3}(?:,\d{3})+(?!\d)', body):
                    return 'Ambiguous commas: separate sum items with a comma and a space; omit thousands separators.'
                parts = body.split(',')
                if not 1 <= len(parts) <= 128:
                    raise ValueError('item_limit')
                values = []
                for part in parts:
                    part = part.strip()
                    match = re.fullmatch(r'([+-]?)(' + NUMBER + ')', part)
                    if not match:
                        raise ValueError('invalid_sum')
                    values.append(numeric(match[2]) * (-1 if match[1] == '-' else 1))
                result = sum(values, Decimal(0))
            else:
                depth = 0
                for character in body:
                    depth += (character == '(') - (character == ')')
                    if depth > 32:
                        raise ValueError('depth_limit')
                tree = ast.parse(body, mode='eval')
                if sum(1 for _ in ast.walk(tree)) > 256:
                    raise ValueError('expression_limit')
                def visit(node, depth=0):
                    if depth > 32:
                        raise ValueError('depth_limit')
                    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                        return numeric(ast.get_source_segment(body, node))
                    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
                        value = visit(node.operand, depth+1)
                        return -value if isinstance(node.op, ast.USub) else value
                    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
                        a, b = visit(node.left, depth+1), visit(node.right, depth+1)
                        if isinstance(node.op, ast.Add): value = a+b
                        elif isinstance(node.op, ast.Sub): value = a-b
                        elif isinstance(node.op, ast.Mult): value = a*b
                        else:
                            if b == 0: raise ZeroDivisionError
                            value = a/b
                        if not value.is_finite() or abs(value) > Decimal('1e100'):
                            raise ValueError('result_limit')
                        return value
                    raise ValueError('unsupported_expression')
                result = visit(tree.body)
            approximate = bool(ctx.flags[Inexact])
            rendered = format(result, 'f')
            if '.' in rendered:
                rendered = rendered.rstrip('0').rstrip('.')
            if result == 0:
                rendered = '0'
            return ('Calculated result (rounded to 50 significant digits): ' if approximate
                    else 'Calculated result: ') + rendered
    except ZeroDivisionError:
        return 'Cannot calculate: division by zero.'
    except (SyntaxError, ValueError, DecimalException, RecursionError):
        return ('Cannot calculate this expression safely. Use numbers with +, -, *, / and parentheses, '
                'or “sum” followed by comma-separated numbers. Numbers are limited to 40 digits.')
