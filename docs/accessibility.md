# Accessibility

The interface uses a shared page shell with a skip link, labelled primary navigation, a `main`
landmark, and visible keyboard focus. Views use headings and labelled sections; form fields use
native controls; loading, saved, and error messages use status or alert roles. Images that add no
information have empty alternative text, and status is communicated with text as well as color.

## Manual checks

For changes to the interface:

- Use Tab and Shift+Tab from a fresh page load. Confirm the skip link, navigation, controls, and
  conditional forms are reachable in a sensible order and have a visible focus indicator.
- Activate links, buttons, and form controls with the keyboard. Confirm conditional forms can be
  opened, submitted, and cancelled without losing the user's place.
- Check form labels, validation errors, and updates announced through status messages.
- Check text and control contrast against their actual backgrounds, including gradients and
  hover/focus states; make sure meaning does not depend on color alone.
- At 200% zoom and a narrow viewport, confirm navigation remains usable and tables and content can
  be read without clipped text.
- Check that meaningful images have useful alternative text and decorative images are ignored.

## Remaining limitations

These checks do not establish screen-reader conformance or replace testing with assistive technology.
Pay particular attention to new dialogs, live updates, charts, and custom controls; prefer native
HTML controls when they meet the need.
