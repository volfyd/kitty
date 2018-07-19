#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2018, Kovid Goyal <kovid at kovidgoyal.net>

from collections import defaultdict


def resolve_keys(keymap):
    ans = defaultdict(list)
    for ch, (attr, atype) in keymap.items():
        if atype not in ('int', 'uint'):
            atype = 'flag'
        ans[atype].append(ch)
    return ans


def enum(keymap):
    lines = []
    for ch, (attr, atype) in keymap.items():
        lines.append(f"{attr}='{ch}'")
    return '''
    enum KEYS {{
        {}
    }};
    '''.format(',\n'.join(lines))


def parse_key(keymap):
    lines = []
    for attr, atype in keymap.values():
        vs = atype.upper() if atype in ('uint', 'int') else 'FLAG'
        lines.append(f'case {attr}: value_state = {vs}; break;')
    return '        \n'.join(lines)


def parse_flag(keymap, type_map, command_class):
    lines = []
    for ch in type_map['flag']:
        attr, allowed_values = keymap[ch]
        q = ' && '.join(f"g.{attr} != '{x}'" for x in allowed_values)
        lines.append(f'''
            case {attr}: {{
                g.{attr} = screen->parser_buf[pos++] & 0xff;
                if ({q}) {{
                    REPORT_ERROR("Malformed {command_class} control block, unknown flag value for {attr}: 0x%x", g.{attr});
                    return;
                }};
            }}
            break;
        ''')
    return '        \n'.join(lines)


def parse_number(keymap):
    int_keys = [f'I({attr})' for attr, atype in keymap.values() if atype == 'int']
    uint_keys = [f'U({attr})' for attr, atype in keymap.values() if atype == 'uint']
    return '; '.join(int_keys), '; '.join(uint_keys)


def cmd_for_report(report_name, keymap, type_map, payload_allowed):
    def group(atype, conv):
        flag_fmt, flag_attrs = [], []
        cv = {'flag': 'c', 'int': 'i', 'uint': 'I'}[atype]
        for ch in type_map[atype]:
            flag_fmt.append('s' + cv)
            attr = keymap[ch][0]
            flag_attrs.append(f'"{attr}", {conv}g.{attr}')
        return ' '.join(flag_fmt), ', '.join(flag_attrs)

    flag_fmt, flag_attrs = group('flag', '')
    int_fmt, int_attrs = group('int', '(int)')
    uint_fmt, uint_attrs = group('uint', '(unsigned int)')

    fmt = f'{flag_fmt} {uint_fmt} {int_fmt}'
    if payload_allowed:
        ans = [f'REPORT_VA_COMMAND("s {{{fmt} sI}} y#", "{report_name}",']
    else:
        ans = [f'REPORT_VA_COMMAND("s {{{fmt}}}", "{report_name}",']
    ans.append(',\n     '.join((flag_attrs, uint_attrs, int_attrs)))
    if payload_allowed:
        ans.append(', "payload_sz", g.payload_sz, payload, g.payload_sz')
    ans.append(');')
    return '\n'.join(ans)


def generate(function_name, callback_name, report_name, keymap, command_class, initial_key='a', payload_allowed=True):
    type_map = resolve_keys(keymap)
    keys_enum = enum(keymap)
    handle_key = parse_key(keymap)
    flag_keys = parse_flag(keymap, type_map, command_class)
    int_keys, uint_keys = parse_number(keymap)
    report_cmd = cmd_for_report(report_name, keymap, type_map, payload_allowed)
    if payload_allowed:
        payload_after_value = "case ';': state = PAYLOAD; break;"
        payload = payload = ', PAYLOAD'
        parr = 'static uint8_t payload[4096];'
        payload_case = f'''
            case PAYLOAD: {{
                sz = screen->parser_buf_pos - pos;
                const char *err = base64_decode(screen->parser_buf + pos, sz, payload, sizeof(payload), &g.payload_sz);
                if (err != NULL) {{ REPORT_ERROR("Failed to parse {command_class} command payload with error: %s", err); return; }}
                pos = screen->parser_buf_pos;
                }}
                break;
        '''
        callback = f'{callback_name}(screen, &g, payload)'
    else:
        payload_after_value = payload = parr = payload_case = ''
        callback = f'{callback_name}(screen, &g)'

    return f'''
static inline void
{function_name}(Screen *screen, PyObject UNUSED *dump_callback) {{
    unsigned int pos = 1;
    enum PARSER_STATES {{ KEY, EQUAL, UINT, INT, FLAG, AFTER_VALUE {payload} }};
    enum PARSER_STATES state = KEY, value_state = FLAG;
    static {command_class} g;
    unsigned int i, code;
    uint64_t lcode;
    bool is_negative;
    memset(&g, 0, sizeof(g));
    size_t sz;
    {parr}
    {keys_enum}
    enum KEYS key = '{initial_key}';

    while (pos < screen->parser_buf_pos) {{
        switch(state) {{
            case KEY:
                key = screen->parser_buf[pos++];
                state = EQUAL;
                switch(key) {{
                    {handle_key}
                    default:
                        REPORT_ERROR("Malformed {command_class} control block, invalid key character: 0x%x", key);
                        return;
                }}
                break;

            case EQUAL:
                if (screen->parser_buf[pos++] != '=') {{
                    REPORT_ERROR("Malformed {command_class} control block, no = after key, found: 0x%x instead", screen->parser_buf[pos-1]);
                    return;
                }}
                state = value_state;
                break;

            case FLAG:
                switch(key) {{
                    {flag_keys}
                    default:
                        break;
                }}
                state = AFTER_VALUE;
                break;

            case INT:
#define READ_UINT \\
                for (i = pos; i < MIN(screen->parser_buf_pos, pos + 10); i++) {{ \\
                    if (screen->parser_buf[i] < '0' || screen->parser_buf[i] > '9') break; \\
                }} \\
                if (i == pos) {{ REPORT_ERROR("Malformed {command_class} control block, expecting an integer value for key: %c", key & 0xFF); return; }} \\
                lcode = utoi(screen->parser_buf + pos, i - pos); pos = i; \\
                if (lcode > UINT32_MAX) {{ REPORT_ERROR("Malformed {command_class} control block, number is too large"); return; }} \\
                code = lcode;

                is_negative = false;
                if(screen->parser_buf[pos] == '-') {{ is_negative = true; pos++; }}
#define I(x) case x: g.x = is_negative ? 0 - (int32_t)code : (int32_t)code; break
                READ_UINT;
                switch(key) {{
                    {int_keys};
                    default: break;
                }}
                state = AFTER_VALUE;
                break;
#undef I
            case UINT:
                READ_UINT;
#define U(x) case x: g.x = code; break
                switch(key) {{
                    {uint_keys};
                    default: break;
                }}
                state = AFTER_VALUE;
                break;
#undef U
#undef READ_UINT

            case AFTER_VALUE:
                switch (screen->parser_buf[pos++]) {{
                    default:
                        REPORT_ERROR("Malformed {command_class} control block, expecting a comma or semi-colon after a value, found: 0x%x",
                                     screen->parser_buf[pos - 1]);
                        return;
                    case ',':
                        state = KEY;
                        break;
                    {payload_after_value}
                }}
                break;

            {payload_case}

        }} // end switch
    }} // end while

    switch(state) {{
        case EQUAL:
            REPORT_ERROR("Malformed {command_class} control block, no = after key"); return;
        case INT:
        case UINT:
            REPORT_ERROR("Malformed {command_class} control block, expecting an integer value"); return;
        case FLAG:
            REPORT_ERROR("Malformed {command_class} control block, expecting a flag value"); return;
        default:
            break;
    }}

    {report_cmd}

    {callback};
}}
    '''


def graphics_parser():
    flag = frozenset
    keymap = {
        'a': ('action', flag('tTqpd')),
        'd': ('delete_action', flag('aAiIcCpPqQxXyYzZ')),
        't': ('transmission_type', flag('dfts')),
        'o': ('compressed', flag('z')),
        'f': ('format', 'uint'),
        'm': ('more', 'uint'),
        'i': ('id', 'uint'),
        'w': ('width', 'uint'),
        'h': ('height', 'uint'),
        'x': ('x_offset', 'uint'),
        'y': ('y_offset', 'uint'),
        'v': ('data_height', 'uint'),
        's': ('data_width', 'uint'),
        'S': ('data_sz', 'uint'),
        'O': ('data_offset', 'uint'),
        'c': ('num_cells', 'uint'),
        'r': ('num_lines', 'uint'),
        'X': ('cell_x_offset', 'uint'),
        'Y': ('cell_y_offset', 'uint'),
        'z': ('z_index', 'int'),
    }
    text = generate('parse_graphics_code', 'screen_handle_graphics_command', 'graphics_command', keymap, 'GraphicsCommand')

    with open('kitty/parse-graphics-command.h', 'w') as f:
        print('#pragma once', file=f)
        print(text, file=f)


graphics_parser()