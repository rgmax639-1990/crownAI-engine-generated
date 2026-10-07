// Single source of truth for brand identity. The Crownwright logo and name
// (from the supplied brand assets) are used for both the company site and
// the Crown AI tool, so the two always share one identity.
export const BRAND_NAME = "Crownwright";
export const BRAND_TAGLINE = "AI Software Delivery";
export const PRODUCT_NAME = "Crown AI";
export const LEGAL_NAME = "Crownwright Technologies Pvt Ltd";
export const ADDRESS_LINES = ["Maraimalar Nagar, Chennai,", "Tamil Nadu, India"];
export const ADDRESS_ONE_LINE = "Maraimalar Nagar, Chennai, Tamil Nadu, India";

export function pageTitle(title: string) {
  return `${title} | ${BRAND_NAME}`;
}
